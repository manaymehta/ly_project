"""
analyst.py

Receives a DisruptionEvent from Sentinel and organises it into
DrugAlertPackages — one per affected drug — ready for agent invocation.

What it does:
    1. Groups flat engine results by drug_id
    2. For each drug group, determines which agents to invoke
    3. Fetches Neo4j context needed by agents in batch queries
    4. Builds DrugAlertPackage dataclasses for actionable drugs
    5. Returns list of packages sorted by overall risk (HIGH first)

What it does NOT do:
    - Call any LLM
    - Make procurement or clinical decisions
    - Write to SQLite
"""

import os
from dataclasses import dataclass, field
from collections import defaultdict
from typing import List, Optional
from neo4j import GraphDatabase
from dotenv import load_dotenv

load_dotenv()

from sentinel import DisruptionEvent
import session_manager

NEO4J_URI      = os.environ["NEO4J_URI"]
NEO4J_USER     = os.environ["NEO4J_USER"]
NEO4J_PASSWORD = os.environ["NEO4J_PASSWORD"]

driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

def _run(cypher, params=None):
    with driver.session() as s:
        return [dict(r) for r in s.run(cypher, params or {})]

# Risk tier ordering for sorting
RISK_ORDER = {"HIGH_RISK": 0, "MEDIUM_RISK": 1, "LOW_RISK": 2, "NO_RISK": 3}


# ── DATACLASSES ────────────────────────────────────────────────────────────────

@dataclass
class HospitalRisk:
    """
    One affected hospital within a drug alert.
    Carries engine scores + available distributor options for this hospital.
    """
    hospital_id:           str
    hospital_name:         str
    hospital_city:         str
    specialty_type:        str
    avg_daily_patients:    int
    shortage_probability:  float
    risk_level:            str
    requires_action:       bool
    days_until_stockout:   float
    prophet_forecast_30d:  float
    time_factor:           float
    distributors:          List[dict] = field(default_factory=list)
    # distributors: list of dicts with keys:
    #   distributor_id, name, city, delivery_speed_class, specialization,
    #   reliability_score, pricing_tier, vulnerability_score,
    #   price_per_unit, min_order, delivery_days, current_stock


@dataclass
class DrugAlertPackage:
    """
    Complete context package for one drug affected by a disruption.
    Passed directly to Procurement Agent and Clinical Agent.
    """
    # ── Drug identity ──────────────────────────────────────────────────────────
    drug_id:            str
    drug_name:          str
    criticality:        str
    category:           str
    vulnerability_score:float   # from gnn_centrality

    # ── Disruption context (same for all hospitals) ────────────────────────────
    disruption_type:       str
    disrupted_node:        str
    disrupted_name:        str
    triggered_date:        str
    recovery_days:         int
    supply_loss_pct:       float
    demand_pressure:       float
    drug_units_remaining:  float
    system_total_forecast: float

    # ── Risk summary ───────────────────────────────────────────────────────────
    overall_risk_level:  str    # highest risk tier across all affected hospitals
    invoke_procurement:  bool   # True if any hospital is MEDIUM or HIGH
    invoke_clinical:     bool   # True if any hospital is HIGH_RISK

    # ── Affected hospitals sorted by urgency (days_until_stockout ascending) ──
    hospitals: List[HospitalRisk] = field(default_factory=list)

    # ── Neo4j context for agents ───────────────────────────────────────────────
    api_context:  dict = field(default_factory=dict)
    # api_context keys: api_id, api_name, api_vulnerability_score

    alternatives: List[dict] = field(default_factory=list)
    # alternatives: list of dicts with keys:
    #   alt_drug_id, alt_drug_name, alt_criticality, similarity_score,
    #   shared_api_risk, substitution_notes

    def summary(self):
        actionable = [h for h in self.hospitals if h.requires_action]
        return (
            f"{self.drug_id} {self.drug_name} [{self.criticality}] | "
            f"{self.overall_risk_level} | "
            f"hospitals={len(self.hospitals)} actionable={len(actionable)} | "
            f"procurement={self.invoke_procurement} clinical={self.invoke_clinical}"
        )


# ── NEO4J CONTEXT QUERIES ─────────────────────────────────────────────────────

def _fetch_drug_context(drug_id: str) -> tuple[dict, List[dict]]:
    """
    Fetches drug vulnerability score, API context, and alternatives.
    Returns (api_context dict, alternatives list).
    Single query — joins Drug→API and Drug→ALTERNATIVE_TO in one call.
    """
    # Drug node + API it uses
    api_rows = _run("""
        MATCH (a:API)-[:COMPONENT_OF]->(d:Drug {id: $did})
        RETURN a.id                AS api_id,
               a.name              AS api_name,
               a.vulnerabilityScore AS api_vulnerability_score
    """, {"did": drug_id})

    api_context = api_rows[0] if api_rows else {}

    # Alternative drugs
    alt_rows = _run("""
        MATCH (d:Drug {id: $did})-[r:ALTERNATIVE_TO]->(alt:Drug)
        RETURN alt.id          AS alt_drug_id,
               alt.name        AS alt_drug_name,
               alt.criticality AS alt_criticality,
               alt.vulnerabilityScore AS alt_vulnerability_score,
               r.similarityScore  AS similarity_score,
               r.sharedApiRisk    AS shared_api_risk,
               r.notes            AS substitution_notes
    """, {"did": drug_id})

    return api_context, alt_rows


def _fetch_hospital_context(hospital_ids: List[str]) -> dict:
    """
    Fetches hospital metadata for all affected hospitals in one query.
    Returns dict keyed by hospital_id.
    """
    rows = _run("""
        MATCH (h:Hospital) WHERE h.id IN $ids
        RETURN h.id              AS hospital_id,
               h.name             AS hospital_name,
               h.city             AS hospital_city,
               h.specialtyType    AS specialty_type,
               h.avgDailyPatients AS avg_daily_patients
    """, {"ids": hospital_ids})

    return {r["hospital_id"]: r for r in rows}


def _fetch_distributor_options(
    drug_id:                str,
    hospital_ids:           List[str],
    exclude_distributor_id: Optional[str] = None,
) -> dict:
    """
    Fetches all distributor options for this drug across all affected
    hospitals in ONE batch query — avoids N queries for N hospitals.
    Returns dict keyed by hospital_id, value is list of distributor dicts.

    exclude_distributor_id: when the disruption is a Distributor node,
    pass its ID here so it is excluded from results. A disrupted
    distributor cannot be recommended as its own replacement.
    """
    exclude_clause = "AND s.id <> $excl_id" if exclude_distributor_id else ""

    rows = _run(f"""
        MATCH (s:Distributor)-[r:DELIVERS_TO]->(h:Hospital)
        WHERE h.id IN $hids AND r.drugId = $did
          AND r.currentStock > 0
          {exclude_clause}
        RETURN h.id                   AS hospital_id,
               s.id                   AS distributor_id,
               s.name                 AS name,
               s.city                 AS city,
               s.deliverySpeedClass   AS delivery_speed_class,
               s.specialization       AS specialization,
               s.reliabilityScore     AS reliability_score,
               s.pricingTier          AS pricing_tier,
               s.vulnerabilityScore   AS vulnerability_score,
               r.pricePerUnit         AS price_per_unit,
               r.minOrder             AS min_order,
               r.deliveryDays         AS delivery_days,
               r.currentStock         AS current_stock
        ORDER BY h.id, r.deliveryDays ASC
    """, {
        "hids":    hospital_ids,
        "did":     drug_id,
        "excl_id": exclude_distributor_id or "",
    })

    # If a simulation session is active, override Neo4j stock values with
    # live depleted values from session.db before grouping.
    for row in rows:
        row["drug_id"] = drug_id          # temp field needed by override
    if session_manager.is_active():
        rows = session_manager.override_analyst_stock(rows)

    # Group by hospital_id — filter rows that became zero after depletion
    by_hospital = defaultdict(list)
    for row in rows:
        hid = row.pop("hospital_id")
        row.pop("drug_id", None)          # clean up temp field
        if row.get("current_stock", 0) > 0:
            by_hospital[hid].append(row)

    return dict(by_hospital)


# ── ANALYST CORE ───────────────────────────────────────────────────────────────

def _determine_overall_risk(hospital_rows: List[dict]) -> str:
    """Returns the highest risk tier across all hospital rows for a drug."""
    tiers = [r["risk_level"] for r in hospital_rows]
    return min(tiers, key=lambda t: RISK_ORDER.get(t, 99))


def _build_drug_alert_package(
    drug_id:       str,
    hospital_rows: List[dict],
    event:         DisruptionEvent,
) -> DrugAlertPackage:
    """
    Builds one DrugAlertPackage from engine results + Neo4j context.
    All Neo4j queries happen here, batched per drug.
    """
    # Take drug-level fields from first row (identical across all rows)
    ref = hospital_rows[0]

    overall_risk    = _determine_overall_risk(hospital_rows)
    invoke_procurement = any(r["requires_action"] for r in hospital_rows)
    invoke_clinical    = any(r["risk_level"] == "HIGH_RISK" for r in hospital_rows)

    hospital_ids = [r["hospital_id"] for r in hospital_rows]

    # ── Batch fetch all Neo4j context ─────────────────────────────────────────
    api_context, alternatives  = _fetch_drug_context(drug_id)
    hospital_meta              = _fetch_hospital_context(hospital_ids)
    # Exclude the disrupted distributor from options when applicable
    excluded_dist = (
        ref["disrupted_node"]
        if ref["disruption_type"] == "Distributor"
        else None
    )
    distributor_options = _fetch_distributor_options(
        drug_id, hospital_ids, exclude_distributor_id=excluded_dist
    )

    # Drug vulnerability score
    drug_vuln = _run("""
        MATCH (d:Drug {id: $did}) RETURN d.vulnerabilityScore AS vs
    """, {"did": drug_id})
    drug_vulnerability = drug_vuln[0]["vs"] if drug_vuln else 0.0

    # ── Build HospitalRisk objects sorted by urgency ──────────────────────────
    # Sorted ascending by days_until_stockout — most urgent first
    sorted_rows = sorted(hospital_rows, key=lambda r: r["days_until_stockout"])

    hospitals = []
    for row in sorted_rows:
        hid  = row["hospital_id"]
        meta = hospital_meta.get(hid, {})
        hospitals.append(HospitalRisk(
            hospital_id          = hid,
            hospital_name        = meta.get("hospital_name", hid),
            hospital_city        = meta.get("hospital_city", ""),
            specialty_type       = meta.get("specialty_type", ""),
            avg_daily_patients   = meta.get("avg_daily_patients", 0),
            shortage_probability = row["shortage_probability"],
            risk_level           = row["risk_level"],
            requires_action      = row["requires_action"],
            days_until_stockout  = row["days_until_stockout"],
            prophet_forecast_30d = row["prophet_forecast_30d"],
            time_factor          = row["time_factor"],
            distributors         = distributor_options.get(hid, []),
        ))

    return DrugAlertPackage(
        drug_id                = drug_id,
        drug_name              = ref["drug_name"],
        criticality            = ref["criticality"],
        category               = ref["category"],
        vulnerability_score    = drug_vulnerability,
        disruption_type        = ref["disruption_type"],
        disrupted_node         = ref["disrupted_node"],
        disrupted_name         = ref["disrupted_name"],
        triggered_date         = event.triggered_date,
        recovery_days          = ref["recovery_days"],
        supply_loss_pct        = ref["supply_loss_pct"],
        demand_pressure        = ref["demand_pressure"],
        drug_units_remaining   = ref["drug_units_remaining"],
        system_total_forecast  = ref["system_total_forecast"],
        overall_risk_level     = overall_risk,
        invoke_procurement     = invoke_procurement,
        invoke_clinical        = invoke_clinical,
        hospitals              = hospitals,
        api_context            = api_context,
        alternatives           = alternatives,
    )


def analyse(event: DisruptionEvent, verbose: bool = True) -> List[DrugAlertPackage]:
    """
    Main Analyst entry point. Takes a DisruptionEvent and returns
    a list of DrugAlertPackages sorted by overall risk level.

    Args:
        event:   DisruptionEvent from Sentinel
        verbose: print progress

    Returns:
        List of DrugAlertPackage — one per affected drug.
        Sorted HIGH_RISK first. Includes LOW_RISK for monitoring log.
    """
    if verbose:
        print(f"\n{'='*62}")
        print(f"  ANALYST — Organising {event.affected_count} results")
        print(f"{'='*62}")

    # ── Group results by drug ──────────────────────────────────────────────────
    by_drug = defaultdict(list)
    for r in event.results:
        by_drug[r["drug_id"]].append(r)

    if verbose:
        print(f"  Unique drugs affected: {len(by_drug)}")

    # ── Build a DrugAlertPackage per drug ─────────────────────────────────────
    packages = []
    for drug_id, rows in by_drug.items():
        pkg = _build_drug_alert_package(drug_id, rows, event)
        packages.append(pkg)
        if verbose:
            print(f"  {pkg.summary()}")

    # ── Sort: HIGH → MEDIUM → LOW → NO_RISK ──────────────────────────────────
    packages.sort(key=lambda p: RISK_ORDER.get(p.overall_risk_level, 99))

    if verbose:
        actionable = [p for p in packages if p.invoke_procurement]
        clinical   = [p for p in packages if p.invoke_clinical]
        monitor    = [p for p in packages if not p.invoke_procurement]
        print(f"\n  Actionable drugs  : {len(actionable)}")
        print(f"  Clinical review   : {len(clinical)}")
        print(f"  Monitor only      : {len(monitor)}")
        print(f"{'='*62}\n")

    return packages


# ── TESTS ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from sentinel import process_disruption
    from prediction_engine import SESSION

    # Test 1: Full pipeline — Cipla Disaster High August
    print("\nTEST 1: Cipla (F002) Disaster / High — August")
    event = process_disruption("Factory", "F002", "Disaster", "High", "2024-08-15")
    packages = analyse(event)

    print(f"\nPackages returned: {len(packages)}")
    for p in packages:
        print(f"\n  {p.drug_name} [{p.criticality}] | {p.overall_risk_level}")
        print(f"    hospitals: {len(p.hospitals)}")
        print(f"    invoke_procurement: {p.invoke_procurement}")
        print(f"    invoke_clinical:    {p.invoke_clinical}")
        print(f"    alternatives:       {len(p.alternatives)}")
        print(f"    api context:        {p.api_context.get('api_name','N/A')}")
        if p.hospitals:
            h = p.hospitals[0]
            print(f"    most urgent:        {h.hospital_name} stockout_in={h.days_until_stockout}d")
            print(f"    distributor count:  {len(h.distributors)}")

    SESSION.reset()

    # Test 2: API disruption — A012 Supply Chain Failure
    print("\n\nTEST 2: API A012 Supply Chain Failure / High — January")
    event2 = process_disruption("API", "A012", "Supply Chain Failure", "High", "2024-01-15")
    packages2 = analyse(event2)

    print(f"\nPackages returned: {len(packages2)}")
    for p in packages2:
        print(f"  {p.drug_name} | {p.overall_risk_level} | "
              f"alternatives={len(p.alternatives)} | "
              f"shared_api_risk={any(a.get('shared_api_risk') for a in p.alternatives)}")

    SESSION.reset()

    # Test 3: Distributor disruption — S003 Logistics Failure Medium
    print("\n\nTEST 3: Distributor S003 Logistics Failure / Medium")
    event3 = process_disruption("Distributor", "S003", "Logistics Failure", "Medium", "2024-08-15")
    packages3 = analyse(event3)

    actionable3 = [p for p in packages3 if p.invoke_procurement]
    print(f"Total packages: {len(packages3)} | Actionable: {len(actionable3)}")
    if actionable3:
        for p in actionable3:
            print(f"  {p.drug_name} | {p.overall_risk_level}")

    SESSION.reset()