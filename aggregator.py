"""
aggregator.py

Aggregator — merges Procurement Agent and Clinical Agent outputs into
one ReviewPackage per drug per disruption, writes to SQLite, and returns
the package ready for the dashboard.

What it does:
    1. Computes per-hospital coverage (units_required vs units_acquired)
       from Procurement Agent option_a allocations
    2. Determines whether Clinical Agent output should be suppressed
       (suppressed when procurement fully covers all hospitals)
    3. Builds a clean ReviewPackage dict with all fields the dashboard needs
    4. Writes to SQLite — one row per package, status=pending_review
    5. Returns list of ReviewPackage dicts sorted HIGH_RISK first

SQLite table: review_packages
    package_id          TEXT PRIMARY KEY
    disruption_node     TEXT
    disruption_event    TEXT
    disruption_severity TEXT
    drug_id             TEXT
    drug_name           TEXT
    criticality         TEXT
    overall_risk_level  TEXT
    procurement_viable  INTEGER
    clinical_suppressed INTEGER
    substitution_viable INTEGER  (NULL if suppressed)
    status              TEXT     (pending_review / approved / rejected)
    created_at          TEXT
    resolved_at         TEXT     (NULL until reviewed)
    procurement_action  TEXT     (JSON of approved order — filled on approval)
    clinical_action     TEXT     (JSON of approved substitution — filled on approval)
    full_package        TEXT     (full JSON blob — dashboard renders this)

Dashboard reads full_package from SQLite to render each review card.
"""

import json
import sqlite3
from datetime import datetime
from typing import Optional

from analyst import DrugAlertPackage

DB_PATH = "disruption_reviews.db"


# ══════════════════════════════════════════════════════════════════════════════
# SQLITE SETUP
# ══════════════════════════════════════════════════════════════════════════════

def init_db(db_path: str = DB_PATH):
    """Create the review_packages table if it doesn't exist."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS review_packages (
            package_id          TEXT PRIMARY KEY,
            disruption_node     TEXT NOT NULL,
            disruption_event    TEXT NOT NULL,
            disruption_severity TEXT NOT NULL,
            drug_id             TEXT NOT NULL,
            drug_name           TEXT NOT NULL,
            criticality         TEXT NOT NULL,
            overall_risk_level  TEXT NOT NULL,
            procurement_viable  INTEGER,
            clinical_suppressed INTEGER,
            substitution_viable INTEGER,
            status              TEXT NOT NULL DEFAULT 'pending_review',
            created_at          TEXT NOT NULL,
            resolved_at         TEXT,
            procurement_action  TEXT,
            clinical_action     TEXT,
            full_package        TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def _write_to_sqlite(package: dict, db_path: str = DB_PATH):
    """Write one ReviewPackage to SQLite. Replaces if package_id already exists."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        INSERT OR REPLACE INTO review_packages (
            package_id, disruption_node, disruption_event, disruption_severity,
            drug_id, drug_name, criticality, overall_risk_level,
            procurement_viable, clinical_suppressed, substitution_viable,
            status, created_at, resolved_at,
            procurement_action, clinical_action, full_package
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        package["package_id"],
        package["disruption"]["node_id"],
        package["disruption"]["event_type"],
        package["disruption"]["severity"],
        package["drug"]["drug_id"],
        package["drug"]["drug_name"],
        package["drug"]["criticality"],
        package["drug"]["overall_risk_level"],
        1 if package["procurement"].get("viable") else 0,
        1 if package["clinical"].get("suppressed") else 0,
        (1 if package["clinical"].get("substitution_viable") else 0)
        if not package["clinical"].get("suppressed") else None,
        package["status"],
        package["created_at"],
        None,   # resolved_at — NULL until reviewed
        None,   # procurement_action — filled on human approval
        None,   # clinical_action — filled on human approval
        json.dumps(package),
    ))
    conn.commit()
    conn.close()


# ══════════════════════════════════════════════════════════════════════════════
# COVERAGE COMPUTATION
# ══════════════════════════════════════════════════════════════════════════════

def _compute_hospital_coverage(
    pkg:              DrugAlertPackage,
    procurement:      dict,
) -> list:
    """
    Computes per-hospital coverage from Procurement Agent option_a allocations.

    For TOTAL_LOSS: reads hospital_allocations from each order in option_a
    For PARTIAL_LOSS: also checks hospitals_covered_by_factory from allocation

    Returns list of dicts — one per actionable hospital — with:
        hospital_id, hospital_name, specialty_type
        risk_level, shortage_probability, days_until_stockout
        units_required, units_acquired, coverage_gap
        coverage_status: FULL | PARTIAL | NONE | COVERED_BY_FACTORY
    """
    # Build units_acquired lookup from option_a allocations
    acquired = {}   # hospital_id → units_acquired
    orders   = procurement.get("option_a") or []

    for order in orders:
        for alloc in order.get("hospital_allocations", []):
            hid = alloc.get("hospital_id", "")
            qty = alloc.get("units_allocated", 0) or 0
            acquired[hid] = acquired.get(hid, 0) + qty

    # For PARTIAL_LOSS: hospitals covered by factory don't need orders
    factory_covered = set()
    allocation = procurement.get("allocation", {})
    if allocation:
        factory_covered = set(
            allocation.get("hospitals_covered_by_factory", []))

    # Build coverage per hospital
    result = []
    for h in pkg.hospitals:
        if not h.requires_action:
            continue

        hid           = h.hospital_id
        units_required = h.prophet_forecast_30d
        units_acquired = acquired.get(hid, 0)
        coverage_gap   = max(0, units_required - units_acquired)

        if hid in factory_covered:
            status = "COVERED_BY_FACTORY"
        elif units_acquired >= units_required:
            status = "FULL"
        elif units_acquired > 0:
            status = "PARTIAL"
        else:
            status = "NONE"

        result.append({
            "hospital_id":          hid,
            "hospital_name":        h.hospital_name,
            "hospital_city":        h.hospital_city,
            "specialty_type":       h.specialty_type,
            "risk_level":           h.risk_level,
            "shortage_probability": h.shortage_probability,
            "days_until_stockout":  h.days_until_stockout,
            "units_required":       round(units_required),
            "units_acquired":       round(units_acquired),
            "coverage_gap":         round(coverage_gap),
            "coverage_pct":         round(
                (units_acquired / units_required * 100) if units_required > 0 else 0, 1),
            "coverage_status":      status,
        })

    # Sort by days_until_stockout ascending — most urgent first
    result.sort(key=lambda x: x["days_until_stockout"])
    return result


def _is_fully_covered(coverage: list) -> bool:
    """Returns True if every actionable hospital is FULL or COVERED_BY_FACTORY.
    An empty coverage list (no hospitals need action) is also considered fully covered.
    """
    return all(
        h["coverage_status"] in ("FULL", "COVERED_BY_FACTORY")
        for h in coverage
    )


# ══════════════════════════════════════════════════════════════════════════════
# ACTION SUMMARY
# ══════════════════════════════════════════════════════════════════════════════

def _build_action_summary(
    pkg:          DrugAlertPackage,
    procurement:  dict,
    clinical:     dict,
    coverage:     list,
    suppressed:   bool,
) -> str:
    """One-line summary for the dashboard alert card."""
    parts = []

    viable = procurement.get("viable") or procurement.get("procurement_viable")
    if not viable:
        parts.append("Procurement not viable")
    else:
        partial = [h for h in coverage if h["coverage_status"] == "PARTIAL"]
        none_   = [h for h in coverage if h["coverage_status"] == "NONE"]
        if none_:
            parts.append(
                f"{len(none_)} hospital(s) uncovered")
        elif partial:
            parts.append(
                f"{len(partial)} hospital(s) partially covered")
        else:
            parts.append("Procurement covers all hospitals")

    if not suppressed:
        if not clinical.get("substitution_viable"):
            parts.append("no viable substitution")
        else:
            parts.append(
                f"substitution available: {clinical.get('recommended_alt_name')}")
        if clinical.get("requires_physician_approval"):
            parts.append("physician sign-off required")

    if procurement.get("is_dicey_case"):
        parts.append("dicey case — two options presented")

    return " | ".join(parts) if parts else "Review required"


# ══════════════════════════════════════════════════════════════════════════════
# PACKAGE BUILDER
# ══════════════════════════════════════════════════════════════════════════════

def _build_review_package(
    pkg:         DrugAlertPackage,
    procurement: dict,
    clinical:    dict,
    event_type:  str,
    severity:    str,
) -> dict:
    """Builds one ReviewPackage dict from all inputs."""

    created_at = datetime.now().isoformat()
    package_id = (
        f"{pkg.disrupted_node}-{pkg.drug_id}-{event_type}-{pkg.triggered_date}"
    )

    # Per-hospital coverage
    coverage = _compute_hospital_coverage(pkg, procurement)

    # Clinical suppression check
    # Suppress if: clinical was not invoked OR procurement fully covers all hospitals
    fully_covered = _is_fully_covered(coverage)
    suppress_clinical = (not pkg.invoke_clinical) or fully_covered
    suppression_reason = None
    if suppress_clinical:
        if not pkg.invoke_clinical:
            suppression_reason = "Drug not HIGH_RISK — clinical review not triggered"
        elif fully_covered:
            suppression_reason = (
                "Procurement fully covers all hospital demand — "
                "substitution not required"
            )

    # Clinical section
    clinical_section = {
        "suppressed":               suppress_clinical,
        "suppression_reason":       suppression_reason,
        "substitution_viable":      clinical.get("substitution_viable") if not suppress_clinical else None,
        "no_alternative_reason":    clinical.get("no_alternative_reason"),
        "recommended_alt_id":       clinical.get("recommended_alt_id"),
        "recommended_alt_name":     clinical.get("recommended_alt_name"),
        "recommended_alt_criticality": clinical.get("recommended_alt_criticality"),
        "similarity_score":         clinical.get("similarity_score"),
        "similarity_tier":          clinical.get("similarity_tier"),
        "substitution_notes":       clinical.get("substitution_notes"),
        "requires_physician_approval": clinical.get("requires_physician_approval", False),
        "viable_alternatives":      clinical.get("viable_alternatives", []),
        "blocked_alternatives":     clinical.get("blocked_alternatives", []),
        "critically_urgent_hospitals": clinical.get("critically_urgent_hospitals", []),
        "system_wide_caveats":      clinical.get("system_wide_caveats", []),
    }

    # Procurement section — normalise both paths to same shape
    procurement_section = {
        "scenario":          procurement.get("scenario"),
        "viable":            procurement.get("procurement_viable", False),
        "call_count":        procurement.get("call_count", 1),
        "is_dicey_case":     procurement.get("is_dicey_case", False),
        "dicey_tradeoff":    procurement.get("dicey_tradeoff"),
        "recommendation_summary": procurement.get("recommendation_summary", ""),
        "option_a":          procurement.get("option_a", []),
        "option_b":          procurement.get("option_b"),
        "total_stock_gap":   procurement.get("total_stock_gap", 0),
        "caveats":           procurement.get("caveats", []),
        # PARTIAL_LOSS specific
        "allocation":        procurement.get("allocation"),
        "bridge_order":      procurement.get("bridge_order"),
    }

    action_summary = _build_action_summary(
        pkg, procurement_section, clinical_section, coverage, suppress_clinical)

    return {
        "package_id":  package_id,
        "status":      "pending_review",
        "created_at":  created_at,
        "action_required": pkg.invoke_procurement or pkg.invoke_clinical,
        "action_summary":  action_summary,

        "disruption": {
            "node_id":       pkg.disrupted_node,
            "node_name":     pkg.disrupted_name,
            "disruption_type": pkg.disruption_type,
            "event_type":    event_type,
            "severity":      severity,
            "recovery_days": pkg.recovery_days,
            "triggered_date":pkg.triggered_date,
        },

        "drug": {
            "drug_id":              pkg.drug_id,
            "drug_name":            pkg.drug_name,
            "criticality":          pkg.criticality,
            "category":             pkg.category,
            "overall_risk_level":   pkg.overall_risk_level,
            "supply_loss_pct":      pkg.supply_loss_pct,
            "demand_pressure":      pkg.demand_pressure,
            "drug_units_remaining": pkg.drug_units_remaining,
            "system_total_forecast":pkg.system_total_forecast,
            "vulnerability_score":  pkg.vulnerability_score,
            "disrupted_api_name": (
                pkg.api_context.get("api_name")
                if pkg.disruption_type != "Distributor"
                else None
            ),
        },

        "hospital_coverage": coverage,

        "procurement": procurement_section,

        "clinical": clinical_section,
    }


# ══════════════════════════════════════════════════════════════════════════════
# MAIN AGGREGATOR FUNCTION
# ══════════════════════════════════════════════════════════════════════════════

def aggregate(
    packages:    list,           # list of DrugAlertPackage
    procurements:dict,           # {drug_id: procurement_result}
    clinicals:   dict,           # {drug_id: clinical_result}
    event_type:  str,
    severity:    str,
    db_path:     str = DB_PATH,
    verbose:     bool = True,
) -> list:
    """
    Main Aggregator entry point.

    Args:
        packages:     List of DrugAlertPackage from Analyst
        procurements: Dict mapping drug_id → Procurement Agent output
        clinicals:    Dict mapping drug_id → Clinical Agent output
        event_type:   e.g. "Disaster", "Supply Chain Failure"
        severity:     "High" | "Medium" | "Low"
        db_path:      SQLite database path
        verbose:      print progress

    Returns:
        List of ReviewPackage dicts sorted by risk tier then shortage_probability.
        Each package is also written to SQLite.
    """
    init_db(db_path)

    if verbose:
        print(f"\n{'='*62}")
        print(f"  AGGREGATOR — Building {len(packages)} review package(s)")
        print(f"{'='*62}")

    # Risk sort order
    risk_order = {"HIGH_RISK": 0, "MEDIUM_RISK": 1, "LOW_RISK": 2, "NO_RISK": 3}

    review_packages = []

    for pkg in packages:
        drug_id      = pkg.drug_id
        procurement  = procurements.get(drug_id, {})
        clinical     = clinicals.get(drug_id, {})

        package = _build_review_package(
            pkg, procurement, clinical, event_type, severity)

        _write_to_sqlite(package, db_path)
        review_packages.append(package)

        if verbose:
            cov = package["hospital_coverage"]
            full    = sum(1 for h in cov if h["coverage_status"] in ("FULL","COVERED_BY_FACTORY"))
            partial = sum(1 for h in cov if h["coverage_status"] == "PARTIAL")
            none_   = sum(1 for h in cov if h["coverage_status"] == "NONE")
            print(
                f"  {pkg.drug_id} {pkg.drug_name:<18} "
                f"[{pkg.overall_risk_level:<12}] "
                f"coverage: {full}✓ {partial}~ {none_}✗ | "
                f"clinical: {'suppressed' if package['clinical']['suppressed'] else 'active'} | "
                f"{package['action_summary'][:60]}"
            )

    # Sort: HIGH_RISK first, then by max shortage_probability within tier
    review_packages.sort(key=lambda p: (
        risk_order.get(p["drug"]["overall_risk_level"], 99),
        -max((h["shortage_probability"] for h in p["hospital_coverage"]), default=0),
    ))

    if verbose:
        actionable = [p for p in review_packages if p["action_required"]]
        print(f"\n  Total packages:    {len(review_packages)}")
        print(f"  Actionable:        {len(actionable)}")
        print(f"  Written to SQLite: {db_path}")
        print(f"{'='*62}\n")

    return review_packages


# ══════════════════════════════════════════════════════════════════════════════
# SQLite QUERY HELPERS (used by FastAPI later)
# ══════════════════════════════════════════════════════════════════════════════

def get_pending_packages(db_path: str = DB_PATH) -> list:
    """Return all pending_review packages as dicts, sorted by risk."""
    conn = sqlite3.connect(db_path)
    rows = conn.execute("""
        SELECT full_package FROM review_packages
        WHERE status = 'pending_review'
        ORDER BY
            CASE overall_risk_level
                WHEN 'HIGH_RISK'   THEN 0
                WHEN 'MEDIUM_RISK' THEN 1
                WHEN 'LOW_RISK'    THEN 2
                ELSE 3
            END,
            created_at DESC
    """).fetchall()
    conn.close()
    return [json.loads(r[0]) for r in rows]


def update_package_status(
    package_id:         str,
    status:             str,   # approved | rejected
    procurement_action: Optional[dict] = None,
    clinical_action:    Optional[dict] = None,
    db_path:            str = DB_PATH,
):
    """Update package status after human review. Called by FastAPI on approval/rejection."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        UPDATE review_packages
        SET status             = ?,
            resolved_at        = ?,
            procurement_action = ?,
            clinical_action    = ?
        WHERE package_id = ?
    """, (
        status,
        datetime.now().isoformat(),
        json.dumps(procurement_action) if procurement_action else None,
        json.dumps(clinical_action)    if clinical_action    else None,
        package_id,
    ))
    conn.commit()
    conn.close()


def get_package_by_id(package_id: str, db_path: str = DB_PATH) -> Optional[dict]:
    """Fetch one package by ID."""
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT full_package FROM review_packages WHERE package_id = ?",
        (package_id,)
    ).fetchone()
    conn.close()
    return json.loads(row[0]) if row else None


# ══════════════════════════════════════════════════════════════════════════════
# TESTS
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import os
    from sentinel import process_disruption
    from analyst import analyse
    from procurement_agent import run_procurement_agent
    from clinical_agent import run_clinical_agent
    from prediction_engine import SESSION

    # Clean test DB
    if os.path.exists("test_reviews.db"):
        os.remove("test_reviews.db")

    print("\nTEST: Full pipeline — Cipla Disaster / High / August")
    print("Sentinel → Analyst → Procurement + Clinical → Aggregator")

    event = process_disruption("Factory", "F002", "Disaster", "High", "2024-08-15")
    packages = analyse(event, verbose=False)

    from concurrent.futures import ThreadPoolExecutor, as_completed
    from procurement_agent import print_procurement_result
    import threading
    import time

    # Run agents in parallel — each drug package is independent.
    # Lock protects the shared result dicts from concurrent writes.
    procurements = {}
    clinicals    = {}
    lock         = threading.Lock()

    def _run_pkg(pkg):
        """Run procurement + clinical for one drug package. Thread-safe."""
        proc_result = None
        clin_result = None

        if pkg.invoke_procurement:
            proc_result = run_procurement_agent(pkg, verbose=False)

        if pkg.invoke_clinical:
            clin_result = run_clinical_agent(pkg, verbose=False)
        else:
            clin_result = {"substitution_viable": None, "suppressed": True}

        return pkg, proc_result, clin_result

    print(f"\n  Running agents in parallel for {len(packages)} package(s)...")

    # Use a single executor context
    with ThreadPoolExecutor(max_workers=len(packages)) as executor:
        futures = {}
        for pkg in packages:
            futures[executor.submit(_run_pkg, pkg)] = pkg
            time.sleep(1.5)   # stagger submissions — avoids burst API calls

        for future in as_completed(futures):
            pkg, proc_result, clin_result = future.result()
            with lock:
                if proc_result is not None:
                    procurements[pkg.drug_id] = proc_result
                if clin_result is not None:
                    clinicals[pkg.drug_id] = clin_result

                # Print immediately under the lock — no interleaving between drugs
                if not pkg.invoke_procurement:
                    print(f"  [—] {pkg.drug_name} ({pkg.drug_id}) done | no procurement needed (monitor only)")
                else:
                    status = "✓" if (proc_result or {}).get("parse_ok") else "✗"
                    print(f"\n  [{status}] {pkg.drug_name} ({pkg.drug_id}) complete")
                    if proc_result:
                        print_procurement_result(proc_result)


    # Aggregate once all results are collected

    review_packages = aggregate(
        packages     = packages,
        procurements = procurements,
        clinicals    = clinicals,
        event_type   = "Disaster",
        severity     = "High",
        db_path      = "test_reviews.db",
    )

    # Verify SQLite write
    pending = get_pending_packages("test_reviews.db")
    print(f"\nPackages in SQLite: {len(pending)}")

    # Show top 3 actionable packages in detail
    actionable = [p for p in review_packages if p["action_required"]]
    print(f"\nTop actionable packages:")
    for p in actionable[:3]:
        print(f"\n  {p['package_id']}")
        print(f"  Drug: {p['drug']['drug_name']} [{p['drug']['overall_risk_level']}]")
        print(f"  Action summary: {p['action_summary']}")
        print(f"  Hospital coverage:")
        for h in p["hospital_coverage"][:3]:
            print(f"    {h['hospital_id']} {h['hospital_name']:<25} "
                  f"need={h['units_required']:>5} "
                  f"get={h['units_acquired']:>5} "
                  f"({h['coverage_status']})")
        proc = p["procurement"]
        print(f"  Procurement viable: {proc['viable']} | "
              f"dicey: {proc['is_dicey_case']} | "
              f"gap: {proc.get('total_stock_gap', 'N/A')}")
        clin = p["clinical"]
        print(f"  Clinical suppressed: {clin['suppressed']} | "
              f"viable: {clin['substitution_viable']} | "
              f"alt: {clin.get('recommended_alt_name', 'none')}")

    # Test status update
    if review_packages:
        test_id = review_packages[0]["package_id"]
        update_package_status(
            test_id, "approved",
            procurement_action={"approved_order": "option_a"},
            db_path="test_reviews.db",
        )
        updated = get_package_by_id(test_id, "test_reviews.db")
        print(f"\n  Status update test: {test_id}")
        conn = sqlite3.connect("test_reviews.db")
        row = conn.execute(
            "SELECT status, resolved_at FROM review_packages WHERE package_id=?",
            (test_id,)
        ).fetchone()
        conn.close()
        print(f"  status={row[0]}  resolved_at={row[1]}")

    SESSION.reset()
    print("\nAggregator test complete.")