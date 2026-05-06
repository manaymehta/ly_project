"""
prediction_engine.py

Core prediction engine for pharmaceutical supply chain disruption detection.

Formula:
    shortage_probability = supply_loss_pct × demand_pressure × time_factor

    supply_loss_pct  — fraction of global drug supply lost (0–1).
                       For Distributor disruptions: forced to 1.0.

    demand_pressure  — system-wide signal computed ONCE per affected drug:
                         sum(all hospital 30d forecasts for that drug)
                         ─────────────────────────────────────────────
                         global remaining supply for that drug
                       Capped at 1.0. All hospitals affected by the same
                       drug share this value — they're competing for the
                       same shrinking pool.

    time_factor      — per-hospital urgency:
                         1 - (days_until_stockout / recovery_days)
                       Clamped 0–1. Hospital runs out before recovery → 1.0.
                       Hospital has more buffer than recovery window → 0.0.

Risk classification (4 tiers):
    NO_RISK     score = 0.00         survives the disruption
    LOW_RISK    0 < score < 0.20     minor exposure, monitor only
    MEDIUM_RISK 0.20 ≤ score < 0.50  significant exposure, queue procurement review
    HIGH_RISK   score ≥ 0.50         severe exposure, queue procurement + clinical review

The dashboard surfaces all tiers visually. Procurement Agent prepares
recommendations for MEDIUM+ rows. Clinical Agent prepares alternative
suggestions for HIGH rows. A human reviews and approves/denies every
recommendation — the system never auto-executes.
"""

import os
import pickle
import copy
import pandas as pd
from datetime import datetime, timedelta
from collections import defaultdict
from neo4j import GraphDatabase


# ── CONFIG ─────────────────────────────────────────────────────────────────────

NEO4J_URI      = "neo4j://127.0.0.1:7687"
NEO4J_USER     = "neo4j"
NEO4J_PASSWORD = "QWEasd123"

MODELS_DIR = "prophet_models"
DATA_DIR   = "./datasets"

# Risk tier thresholds — NO_RISK is exactly 0; ranges are [low, high)
RISK_LOW_MIN    = 0.001   # any positive score
RISK_MEDIUM_MIN = 0.20
RISK_HIGH_MIN   = 0.50


def classify_risk(score):
    if score < RISK_LOW_MIN:    return "NO_RISK"
    if score < RISK_MEDIUM_MIN: return "LOW_RISK"
    if score < RISK_HIGH_MIN:   return "MEDIUM_RISK"
    return "HIGH_RISK"


def requires_action(risk_level):
    return risk_level in ("MEDIUM_RISK", "HIGH_RISK")


# ── DISRUPTION TAXONOMY ────────────────────────────────────────────────────────
# Loaded once at startup. Recovery days = midpoint of (Min Days, Max Days).
# Falls back to severity defaults if combination not found.

def load_taxonomy(path="datasets/disruption_taxonomy.csv"):
    taxonomy = {}
    try:
        df = pd.read_csv(path)
        df.columns = df.columns.str.strip()
        for _, row in df.iterrows():
            key = (row["Node Type"].strip(),
                   row["Event Type"].strip(),
                   row["Severity"].strip())
            taxonomy[key] = (int(row["Min Days"]), int(row["Max Days"]))
        print(f"Taxonomy loaded: {len(taxonomy)} entries from {path}")
    except FileNotFoundError:
        print(f"WARNING: {path} not found — using severity defaults only.")
    return taxonomy


DISRUPTION_TAXONOMY = load_taxonomy()


def get_recovery_days(node_type, event_type, severity):
    key = (node_type, event_type, severity)
    if key in DISRUPTION_TAXONOMY:
        low, high = DISRUPTION_TAXONOMY[key]
        return int((low + high) / 2)
    return {"High": 21, "Medium": 10, "Low": 4}.get(severity, 14)


# ── BASE DATA ──────────────────────────────────────────────────────────────────
# Neo4j   → graph structure + properties for traversal and scoring
# CSVs    → flat reference (inventory, monthly demand fallback)
# Session → simulation state (mutable inventory, factory status)
# SQLite  → decision log (Phase 2, not yet)

def load_base_data():
    drugs_df     = pd.read_csv(f"{DATA_DIR}/drugs.csv",                 encoding="cp1252")
    hospitals_df = pd.read_csv(f"{DATA_DIR}/hospitals.csv",             encoding="cp1252")
    inventory_df = pd.read_csv(f"{DATA_DIR}/hospital_inventory.csv",    encoding="cp1252")
    demand_df    = pd.read_csv(f"{DATA_DIR}/hospital_drug_demand.csv",  encoding="cp1252",
                               usecols=range(6))

    for df in [drugs_df, hospitals_df, inventory_df, demand_df]:
        df.columns = df.columns.str.strip()

    drug_ref     = drugs_df.set_index("Drug ID").to_dict(orient="index")
    hospital_ref = (hospitals_df.dropna(subset=["Hospital ID"])
                                .set_index("Hospital ID")
                                .to_dict(orient="index"))

    inventory_base = {}
    for _, row in inventory_df.iterrows():
        inventory_base[(str(row["Hospital ID"]).strip(), str(row["Drug ID"]).strip())] = {
            "daily_demand":  float(row["Daily Demand"]),
            "current_units": int(row["Current Units"]),
            "days_of_stock": float(row["Days of Stock"]),
        }

    demand_ref = {}
    for _, row in demand_df.iterrows():
        demand_ref[(str(row["Hospital ID"]).strip(), str(row["Drug ID"]).strip())] = \
            float(row["Monthly Demand"])

    return {
        "drug":      drug_ref,
        "hospital":  hospital_ref,
        "inventory": inventory_base,
        "demand":    demand_ref,
    }


BASE_DATA = load_base_data()


# ── SESSION STATE ──────────────────────────────────────────────────────────────

class SimulationSession:
    def __init__(self):
        self.reset()

    def reset(self):
        self.inventory          = copy.deepcopy(BASE_DATA["inventory"])
        self.factory_status     = {}
        self.active_disruptions = []
        print("Session reset — base state restored.")

    def get_inventory(self, hosp_id, drug_id):
        return self.inventory.get((hosp_id, drug_id))

    def deplete_inventory(self, hosp_id, drug_id, units):
        key = (hosp_id, drug_id)
        if key in self.inventory:
            self.inventory[key]["current_units"] = max(
                0, self.inventory[key]["current_units"] - int(units))

    def restock_inventory(self, hosp_id, drug_id, units):
        key = (hosp_id, drug_id)
        if key in self.inventory:
            self.inventory[key]["current_units"] += int(units)

    def set_factory_offline(self, fid): self.factory_status[fid] = "offline"
    def set_factory_online(self,  fid): self.factory_status[fid] = "online"
    def is_factory_offline(self,  fid): return self.factory_status.get(fid) == "offline"


SESSION = SimulationSession()


# ── NEO4J ──────────────────────────────────────────────────────────────────────

driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))


def neo4j_query(cypher, params=None):
    with driver.session() as s:
        return [dict(r) for r in s.run(cypher, params or {})]


# ── GRAPH TRAVERSAL ────────────────────────────────────────────────────────────

def _hospitals_for_drug(drug_id):
    return [h for (h, d) in SESSION.inventory if d == drug_id]


def get_affected_pairs(node_type, node_id, recovery_days):
    """
    Build list of (hospital, drug) pairs affected by a disruption.
    Each pair carries supply data needed for scoring.
    """
    affected = []

    # ── FACTORY DISRUPTION ────────────────────────────────────────────────────
    if node_type == "Factory":
        SESSION.set_factory_offline(node_id)

        factory_data = neo4j_query("""
            MATCH (f:Factory {id: $id}) RETURN f.name AS name
        """, {"id": node_id})
        if not factory_data:
            print(f"Factory {node_id} not found in Neo4j.")
            return []
        factory_name = factory_data[0]["name"]

        # Single batch query: drugs this factory makes + supply data
        rows = neo4j_query("""
            MATCH (f:Factory {id: $id})-[r:PRODUCES_API]->(a:API)-[c:COMPONENT_OF]->(d:Drug)
            WITH f, r, a, c, d
            MATCH (allf:Factory)-[ar:PRODUCES_API]->(a)
            WITH f, r, c, d, a,
                 sum(allf.monthlyCapacity * ar.capacityShare) AS global_total
            RETURN d.id                                  AS drug_id,
                   d.name                                AS drug_name,
                   d.criticality                         AS criticality,
                   d.category                            AS category,
                   a.id                                  AS api_id,
                   a.name                                AS api_name,
                   (f.monthlyCapacity * r.capacityShare) AS this_factory_output,
                   global_total                          AS global_total,
                   c.yieldMultiplier                     AS yield_multiplier
        """, {"id": node_id})

        for row in rows:
            drug_id = row["drug_id"]
            for hosp_id in _hospitals_for_drug(drug_id):
                affected.append({
                    "drug_id":         drug_id,
                    "drug_name":       row["drug_name"],
                    "hospital_id":     hosp_id,
                    "api_id":          row["api_id"],
                    "api_name":        row["api_name"],
                    "api_units_lost":  row["this_factory_output"],
                    "total_api_supply":row["global_total"],
                    "yield_multiplier":row["yield_multiplier"],
                    "recovery_days":   recovery_days,
                    "criticality":     row["criticality"],
                    "category":        row["category"],
                    "disruption_type": "Factory",
                    "disrupted_node":  node_id,
                    "disrupted_name":  factory_name,
                })

    # ── API DISRUPTION ────────────────────────────────────────────────────────
    elif node_type == "API":
        api_data = neo4j_query("""
            MATCH (a:API {id: $id}) RETURN a.name AS api_name
        """, {"id": node_id})
        if not api_data:
            print(f"API {node_id} not found in Neo4j.")
            return []
        api_name = api_data[0]["api_name"]

        factory_rows = neo4j_query("""
            MATCH (f:Factory)-[r:PRODUCES_API]->(a:API {id: $id})
            RETURN f.id                                  AS factory_id,
                   (f.monthlyCapacity * r.capacityShare) AS factory_output
        """, {"id": node_id})

        global_total = sum(r["factory_output"] for r in factory_rows)

        # Partial-loss handling: count only OFFLINE factories
        units_lost = sum(
            r["factory_output"]
            for r in factory_rows
            if SESSION.is_factory_offline(r["factory_id"])
        )
        # Direct API event with no factories pre-marked → full supply lost
        if units_lost == 0:
            units_lost = global_total

        drugs_hit = neo4j_query("""
            MATCH (a:API {id: $id})-[c:COMPONENT_OF]->(d:Drug)
            RETURN d.id              AS drug_id,
                   d.name             AS drug_name,
                   d.criticality      AS criticality,
                   d.category         AS category,
                   c.yieldMultiplier  AS yield_multiplier
        """, {"id": node_id})

        for dr in drugs_hit:
            drug_id = dr["drug_id"]
            for hosp_id in _hospitals_for_drug(drug_id):
                affected.append({
                    "drug_id":         drug_id,
                    "drug_name":       dr["drug_name"],
                    "hospital_id":     hosp_id,
                    "api_id":          node_id,
                    "api_name":        api_name,
                    "api_units_lost":  units_lost,
                    "total_api_supply":global_total,
                    "yield_multiplier":dr["yield_multiplier"],
                    "recovery_days":   recovery_days,
                    "criticality":     dr["criticality"],
                    "category":        dr["category"],
                    "disruption_type": "API",
                    "disrupted_node":  node_id,
                    "disrupted_name":  api_name,
                })

    # ── DISTRIBUTOR DISRUPTION ────────────────────────────────────────────────
    elif node_type == "Distributor":
        dist_data = neo4j_query("""
            MATCH (d:Distributor {id: $id}) RETURN d.name AS dist_name
        """, {"id": node_id})
        dist_name = dist_data[0]["dist_name"] if dist_data else node_id

        # Fetch the disrupted distributor's own stock per (drug, hospital)
        deliveries = neo4j_query("""
            MATCH (dist:Distributor {id: $id})-[r:DELIVERS_TO]->(h:Hospital)
            RETURN r.drugId AS drug_id, h.id AS hospital_id,
                   r.currentStock AS dist_stock
        """, {"id": node_id})

        unique_drug_ids = list({row["drug_id"] for row in deliveries})
        drug_meta = {}
        if unique_drug_ids:
            rows = neo4j_query("""
                MATCH (d:Drug) WHERE d.id IN $ids
                RETURN d.id          AS drug_id,
                       d.name        AS drug_name,
                       d.criticality AS criticality,
                       d.category    AS category
            """, {"ids": unique_drug_ids})
            drug_meta = {r["drug_id"]: r for r in rows}

        # Fetch total stock for each (drug, hospital) across ALL distributors
        # so we can compute the disrupted distributor's actual share of supply
        unique_hosp_ids = list({row["hospital_id"] for row in deliveries})
        total_stock_rows = neo4j_query("""
            MATCH (dist:Distributor)-[r:DELIVERS_TO]->(h:Hospital)
            WHERE r.drugId IN $drug_ids AND h.id IN $hosp_ids
            RETURN r.drugId AS drug_id, h.id AS hospital_id,
                   SUM(r.currentStock) AS total_stock
        """, {"drug_ids": unique_drug_ids, "hosp_ids": unique_hosp_ids})

        # Build lookup: (drug_id, hospital_id) → total stock across all distributors
        total_stock_map = {
            (r["drug_id"], r["hospital_id"]): float(r["total_stock"] or 0)
            for r in total_stock_rows
        }

        for row in deliveries:
            drug_id    = row["drug_id"]
            hospital_id = row["hospital_id"]
            if drug_id not in drug_meta:
                continue
            dm         = drug_meta[drug_id]
            dist_stock = float(row["dist_stock"] or 0)
            total_stock = total_stock_map.get((drug_id, hospital_id), dist_stock)

            affected.append({
                "drug_id":         drug_id,
                "drug_name":       dm["drug_name"],
                "hospital_id":     hospital_id,
                "api_id":          None,
                "api_name":        None,
                "api_units_lost":  dist_stock,    # this distributor's stock lost
                "total_api_supply":total_stock,   # total supply across all distributors
                "yield_multiplier":1.0,
                "recovery_days":   recovery_days,
                "criticality":     dm["criticality"],
                "category":        dm["category"],
                "disruption_type": "Distributor",
                "disrupted_node":  node_id,
                "disrupted_name":  dist_name,
            })

    return affected


# ── PROPHET FORECAST ───────────────────────────────────────────────────────────

def get_prophet_forecast(hosp_id, drug_id, from_date_str, days=30):
    model_path = f"{MODELS_DIR}/{hosp_id}_{drug_id}.pkl"
    if not os.path.exists(model_path):
        return BASE_DATA["demand"].get((hosp_id, drug_id), 0.0)

    with open(model_path, "rb") as f:
        model = pickle.load(f)

    from_date    = pd.to_datetime(from_date_str)
    last_trained = pd.to_datetime("2024-12-31")
    extra_periods = max(days,
                        int((from_date + timedelta(days=days) - last_trained).days) + 10)

    future   = model.make_future_dataframe(periods=extra_periods, freq="D")
    forecast = model.predict(future)

    window = forecast[
        (forecast["ds"] >= from_date) &
        (forecast["ds"] <  from_date + timedelta(days=days))
    ]
    if window.empty:
        return BASE_DATA["demand"].get((hosp_id, drug_id), 0.0)

    return float(round(window["yhat"].clip(lower=0).sum()))


# ── DRUG-LEVEL SUPPLY/DEMAND AGGREGATION ───────────────────────────────────────
# Called once per disruption to compute system-wide demand_pressure per drug.

def compute_drug_level_metrics(affected, from_date_str):
    """
    For each unique drug in the affected list, compute:
      - drug_units_remaining: global remaining supply (drug units/month)
      - total_system_forecast: sum of 30d forecasts across all affected hospitals
      - demand_pressure: capped ratio
      - per_hospital_forecasts: {hosp_id: forecast} for use in score output

    Returns: dict keyed by drug_id with all these values.
    """
    metrics = {}

    # Group affected pairs by drug
    pairs_by_drug = defaultdict(list)
    for pair in affected:
        pairs_by_drug[pair["drug_id"]].append(pair)

    for drug_id, pairs in pairs_by_drug.items():
        sample = pairs[0]

        # Remaining drug supply
        if sample["disruption_type"] == "Distributor":
            drug_units_remaining = 0.0
        else:
            ym = sample["yield_multiplier"]
            drug_units_total = sample["total_api_supply"] * ym
            drug_units_lost  = sample["api_units_lost"]   * ym
            drug_units_remaining = max(0.0, drug_units_total - drug_units_lost)

        # System-wide demand: sum of forecasts for every affected hospital for this drug
        per_hospital = {}
        total_forecast = 0.0
        for p in pairs:
            f = get_prophet_forecast(p["hospital_id"], drug_id, from_date_str)
            per_hospital[p["hospital_id"]] = f
            total_forecast += f

        demand_pressure = min(1.0, total_forecast / max(drug_units_remaining, 1.0))

        metrics[drug_id] = {
            "drug_units_remaining":  drug_units_remaining,
            "total_system_forecast": total_forecast,
            "demand_pressure":       demand_pressure,
            "per_hospital_forecasts":per_hospital,
        }

    return metrics


# ── SHORTAGE PROBABILITY ───────────────────────────────────────────────────────

def calculate_shortage_probability(pair, drug_metrics):
    """
    Compute shortage_probability for a single (hospital, drug) pair using
    pre-computed drug-level metrics (demand_pressure, per-hospital forecast).
    """
    drug_id = pair["drug_id"]
    hosp_id = pair["hospital_id"]

    inv = SESSION.get_inventory(hosp_id, drug_id)
    if not inv:
        return None

    current_units = inv["current_units"]
    daily_demand  = inv["daily_demand"]
    recovery_days = pair["recovery_days"]

    # Supply Loss — for Distributor disruptions, api_units_lost = disrupted
    # distributor's stock and total_api_supply = sum across all distributors,
    # giving the true stock-share fraction rather than a hardcoded 1.0
    ym = pair["yield_multiplier"]
    drug_units_lost  = pair["api_units_lost"]   * ym
    drug_units_total = pair["total_api_supply"] * ym
    supply_loss_pct = min(1.0, drug_units_lost / max(drug_units_total, 1.0))

    # Demand Pressure (system-wide, shared across all hospitals affected by this drug)
    metrics = drug_metrics[drug_id]
    demand_pressure = metrics["demand_pressure"]
    forecast_30d    = metrics["per_hospital_forecasts"].get(hosp_id, 0.0)

    # Time Factor
    # days_until_stockout = current_units / max(daily_demand, 0.1)

    # Use Prophet-forecasted daily rate for this hospital-drug pair
    # Falls back to static daily_demand if forecast unavailable
    forecast_daily_rate = forecast_30d / 30.0
    effective_daily = forecast_daily_rate if forecast_daily_rate > 0 else daily_demand
    days_until_stockout = current_units / max(effective_daily, 0.1)

    time_factor = max(0.0, min(1.0,
        1.0 - (days_until_stockout / max(recovery_days, 1))
    ))

    # Final score
    probability = round(supply_loss_pct * demand_pressure * time_factor, 3)
    risk_level  = classify_risk(probability)

    return {
        "drug_id":               drug_id,
        "drug_name":             pair["drug_name"],
        "hospital_id":           hosp_id,
        "criticality":           pair["criticality"],
        "category":              pair["category"],
        "disruption_type":       pair["disruption_type"],
        "disrupted_node":        pair["disrupted_node"],
        "disrupted_name":        pair["disrupted_name"],
        "shortage_probability":  probability,
        "risk_level":            risk_level,
        "requires_action":       requires_action(risk_level),
        "supply_loss_pct":       round(supply_loss_pct, 3),
        "demand_pressure":       round(demand_pressure, 3),
        "time_factor":           round(time_factor, 3),
        "days_until_stockout":   round(days_until_stockout, 1),
        "prophet_forecast_30d":  forecast_30d,
        "drug_units_remaining":  round(metrics["drug_units_remaining"], 1),
        "system_total_forecast": round(metrics["total_system_forecast"], 1),
        "recovery_days":         recovery_days,
    }


# ── MAIN PIPELINE ──────────────────────────────────────────────────────────────

def run_prediction_pipeline(node_type, node_id, event_type, severity,
                             triggered_date=None):
    """
    Two-pass pipeline:
      1. Build affected pairs via graph traversal
      2. Compute drug-level metrics (system-wide demand_pressure per drug)
      3. Score each pair using the precomputed drug-level metrics
      4. Group by risk tier for output

    Returns list of result dicts sorted by shortage_probability descending.
    """
    if triggered_date is None:
        triggered_date = datetime.now().strftime("%Y-%m-%d")

    recovery_days = get_recovery_days(node_type, event_type, severity)

    print(f"\n{'='*62}")
    print(f"  DISRUPTION: {node_type} {node_id} | {event_type} ({severity})")
    print(f"  Recovery  : ~{recovery_days} days | Date: {triggered_date}")
    print(f"{'='*62}")

    # Pass 1: graph traversal
    affected = get_affected_pairs(node_type, node_id, recovery_days)
    print(f"  Affected (hospital, drug) pairs: {len(affected)}")
    if not affected:
        return []

    # Pass 2: drug-level system metrics (Prophet forecasts + demand_pressure)
    drug_metrics = compute_drug_level_metrics(affected, triggered_date)

    # Pass 3: score each pair
    results = sorted(
        filter(None, [calculate_shortage_probability(p, drug_metrics) for p in affected]),
        key=lambda x: x["shortage_probability"],
        reverse=True,
    )

    # Group by risk tier
    by_tier = {"HIGH_RISK": [], "MEDIUM_RISK": [], "LOW_RISK": [], "NO_RISK": []}
    for r in results:
        by_tier[r["risk_level"]].append(r)

    print(f"  Risk distribution:")
    print(f"    HIGH_RISK   : {len(by_tier['HIGH_RISK'])}   "
          f"(procurement + clinical review)")
    print(f"    MEDIUM_RISK : {len(by_tier['MEDIUM_RISK'])}   "
          f"(procurement review)")
    print(f"    LOW_RISK    : {len(by_tier['LOW_RISK'])}   (monitor)")
    print(f"    NO_RISK     : {len(by_tier['NO_RISK'])}\n")

    # Show details for actionable tiers
    for tier in ("HIGH_RISK", "MEDIUM_RISK"):
        if not by_tier[tier]:
            continue
        print(f"  --- {tier} ---")
        for r in by_tier[tier][:15]:
            print(
                f"    {r['drug_name']:<26} @ {r['hospital_id']}  "
                f"score={r['shortage_probability']:.3f}  "
                f"[{r['criticality']:<13}]  "
                f"stockout_in={r['days_until_stockout']}d  "
                f"recovery={r['recovery_days']}d"
            )
        if len(by_tier[tier]) > 15:
            print(f"    ... and {len(by_tier[tier]) - 15} more")
        print()

    return results


# ── TESTS ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":

    # Test 1: Cipla disaster in August (monsoon, antibiotic peak)
    print("\nTEST 1: Cipla (F002) Disaster / High — August (monsoon peak)")
    aug = run_prediction_pipeline("Factory", "F002", "Disaster", "High", "2024-08-15")

    SESSION.reset()

    # Test 2: Same disruption in April (seasonal trough)
    print("\nTEST 2: Cipla (F002) Disaster / High — April (seasonal trough)")
    apr = run_prediction_pipeline("Factory", "F002", "Disaster", "High", "2024-04-15")

    a = next((r["shortage_probability"] for r in aug
              if r["drug_id"] == "D004" and r["hospital_id"] == "H001"), None)
    b = next((r["shortage_probability"] for r in apr
              if r["drug_id"] == "D004" and r["hospital_id"] == "H001"), None)
    if a is not None and b is not None:
        verdict = "PASS ✓" if a > b else "FAIL ✗"
        print(f"  Seasonal check — Amoxicillin @ H001: Aug={a:.3f}  Apr={b:.3f}  {verdict}")
    else:
        print("  Seasonal check: no Prophet models — using flat fallback")

    SESSION.reset()

    # Test 3: Salbutamol API single-source loss — January winter peak
    # supply_loss_pct = 1.0, demand_pressure = 1.0 (remaining=0).
    # Score should be purely time_factor — differentiated per hospital.
    print("\nTEST 3: API A012 (Salbutamol) Supply Chain Failure / High — January (winter peak)")
    run_prediction_pipeline("API", "A012", "Supply Chain Failure", "High", "2024-01-15")

    SESSION.reset()

    # Test 4: Distributor S003 minor logistics failure
    # Recovery 6d ≪ all hospital buffers → time_factor near 0 → mostly NO_RISK
    print("\nTEST 4: Distributor S003 Logistics Failure / Medium")
    run_prediction_pipeline("Distributor", "S003", "Logistics Failure", "Medium", "2024-08-15")

    SESSION.reset()

    # Test 5: Compound — Cipla offline first, then API A004 event
    # A004 has 2 producers (Cipla 72%, Lupin 28%). With only Cipla offline,
    # supply_loss_pct ≈ 0.72 (partial loss), system has 28% remaining.
    print("\nTEST 5: API A004 Raw Material Shortage / High — Cipla already offline")
    SESSION.set_factory_offline("F002")
    run_prediction_pipeline("API", "A004", "Raw Material Shortage", "High", "2024-08-15")

    SESSION.reset()