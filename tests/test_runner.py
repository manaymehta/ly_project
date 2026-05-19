"""
test_runner.py

Interactive test runner for the full LY disruption pipeline.
Run with:  python test_runner.py

Pick a scenario by number and the full pipeline runs:
  Sentinel → Analyst → Procurement + Clinical → Aggregator

Each run writes to its own SQLite DB so runs don't interfere.
Raw LLM outputs land in raw_output/ as usual.
"""

import os
import sys
import time
import threading
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "core"))
from concurrent.futures import ThreadPoolExecutor, as_completed

from sentinel import process_disruption, SentinelError
from analyst import analyse
from procurement_agent import run_procurement_agent, print_procurement_result
from clinical_agent import run_clinical_agent
from aggregator import aggregate, get_pending_packages
from prediction_engine import SESSION


# ── Test case definitions ──────────────────────────────────────────────────────

CASES = [
    {
        "num":         1,
        "title":       "Factory F002 (Cipla) — Strike / High",
        "description": "Same Cipla factory as baseline, but a labour strike instead of disaster.\n"
                       "Shorter recovery (~14-21d). Tests how fewer affected days changes\n"
                       "which hospitals cross the HIGH/MEDIUM threshold.",
        "node_type":   "Factory",
        "node_id":     "F002",
        "event_type":  "Strike",
        "severity":    "High",
        "date":        "2024-08-15",
        "db":          "test_F002_Strike_High.db",
    },
    {
        "num":         2,
        "title":       "Factory F001 — Disaster / High",
        "description": "Different factory — completely different drug portfolio from Cipla.\n"
                       "Tests procurement generality across a new set of drugs and distributors.",
        "node_type":   "Factory",
        "node_id":     "F001",
        "event_type":  "Disaster",
        "severity":    "High",
        "date":        "2024-08-15",
        "db":          "test_F001_Disaster_High.db",
    },
    {
        "num":         3,
        "title":       "API A012 — Supply Chain Failure / High",
        "description": "Upstream API disruption. Affects all factories that use A012 as an input.\n"
                       "Can cascade to multiple drugs across multiple factories simultaneously.\n"
                       "Highest complexity — tests multi-drug parallel procurement.",
        "node_type":   "API",
        "node_id":     "A012",
        "event_type":  "Supply Chain Failure",
        "severity":    "High",
        "date":        "2024-01-15",
        "db":          "test_A012_SCF_High.db",
    },
    {
        "num":         4,
        "title":       "API A004 — Raw Material Shortage / High",
        "description": "Different API (A004) disruption. Likely hits different drugs than A012.\n"
                       "Good for seeing how the system handles a narrower upstream failure.",
        "node_type":   "API",
        "node_id":     "A004",
        "event_type":  "Raw Material Shortage",
        "severity":    "High",
        "date":        "2024-08-15",
        "db":          "test_A004_RMS_High.db",
    },
    {
        "num":         5,
        "title":       "Factory F004 — Disaster / High",
        "description": "Third factory — smaller or different portfolio.\n"
                       "Tests a potentially leaner procurement run where only 1-2 drugs\n"
                       "hit the actionable threshold.",
        "node_type":   "Factory",
        "node_id":     "F004",
        "event_type":  "Disaster",
        "severity":    "High",
        "date":        "2024-08-15",
        "db":          "test_F004_Disaster_High.db",
    },
    {
        "num":         6,
        "title":       "Distributor S003 — Logistics Failure / Medium",
        "description": "Distributor-level disruption, Medium severity.\n"
                       "S003 goes offline — hospitals lose one supply route.\n"
                       "Tests re-routing logic and whether remaining distributors cover the gap.",
        "node_type":   "Distributor",
        "node_id":     "S003",
        "event_type":  "Logistics Failure",
        "severity":    "Medium",
        "date":        "2024-08-15",
        "db":          "test_S003_LogFail_Medium.db",
    },
]


# ── Menu ───────────────────────────────────────────────────────────────────────

def print_menu():
    print("\n" + "=" * 62)
    print("  LY PROJECT — DISRUPTION PIPELINE TEST RUNNER")
    print("=" * 62)
    for c in CASES:
        print(f"\n  [{c['num']}] {c['title']}")
        for line in c["description"].splitlines():
            print(f"       {line}")
    print("\n" + "=" * 62)
    print("  [0] Exit")
    print("=" * 62)


def pick_case() -> dict | None:
    while True:
        try:
            raw = input("\n  Enter case number: ").strip()
            n   = int(raw)
        except (ValueError, EOFError):
            print("  Please enter a number.")
            continue
        if n == 0:
            print("  Exiting.")
            return None
        match = next((c for c in CASES if c["num"] == n), None)
        if match:
            return match
        print(f"  Invalid choice '{n}'. Pick 0–{len(CASES)}.")


# ── Pipeline runner ────────────────────────────────────────────────────────────

def run_pipeline(case: dict):
    print(f"\n{'='*62}")
    print(f"  RUNNING: {case['title']}")
    print(f"  Sentinel → Analyst → Agents → Aggregator")
    print(f"{'='*62}\n")

    # Clean DB for this run
    if os.path.exists(case["db"]):
        os.remove(case["db"])

    # ── Sentinel ──
    try:
        event = process_disruption(
            case["node_type"],
            case["node_id"],
            case["event_type"],
            case["severity"],
            case["date"],
        )
    except SentinelError as e:
        print(f"\n  [!] Sentinel hard-stop: {e}")
        SESSION.reset()
        return

    # ── Analyst ──
    packages = analyse(event, verbose=False)

    actionable = [p for p in packages if p.invoke_procurement or p.invoke_clinical]
    if not actionable:
        print(f"\n  No actionable packages — all drugs are LOW_RISK or NO_RISK.")
        SESSION.reset()
        return

    # ── Parallel agents ──
    lock         = threading.Lock()
    procurements = {}
    clinicals    = {}

    def _run_pkg(pkg):
        proc_result = None
        clin_result = None

        if pkg.invoke_procurement:
            t0 = time.time()
            print(f"  [→] {pkg.drug_name} ({pkg.drug_id}) — Call 1 starting")
            proc_result = run_procurement_agent(pkg, verbose=False)
            print(f"  [←] {pkg.drug_name} ({pkg.drug_id}) — done in {time.time()-t0:.1f}s")

        if pkg.invoke_clinical:
            clin_result = run_clinical_agent(pkg, verbose=False)
        else:
            clin_result = {"substitution_viable": None, "suppressed": True}

        return pkg, proc_result, clin_result

    # Separate actionable (need LLM) from monitor-only (no calls needed)
    llm_pkgs     = [p for p in packages if p.invoke_procurement or p.invoke_clinical]
    monitor_pkgs = [p for p in packages if not p.invoke_procurement and not p.invoke_clinical]

    # Print monitor-only immediately — no thread needed
    for pkg in monitor_pkgs:
        print(f"  [—] {pkg.drug_name} ({pkg.drug_id}) — monitor only")

    print(f"\n  Running agents in parallel for {len(llm_pkgs)} package(s) (LLM)...\n")

    with ThreadPoolExecutor(max_workers=max(1, len(llm_pkgs))) as executor:
        futures = {}
        for pkg in llm_pkgs:
            futures[executor.submit(_run_pkg, pkg)] = pkg
            if pkg.invoke_procurement:
                time.sleep(10)   # stagger LLM callers to avoid burst API calls

        for future in as_completed(futures):
            failed_pkg = futures[future]
            try:
                pkg, proc_result, clin_result = future.result()
            except Exception as exc:
                # One drug failing must NOT crash the rest of the pipeline.
                print(f"\n  [✗] {failed_pkg.drug_name} ({failed_pkg.drug_id}) — LLM call failed: {type(exc).__name__}: {exc}")
                continue
            with lock:
                if proc_result is not None:
                    procurements[pkg.drug_id] = proc_result
                if clin_result is not None:
                    clinicals[pkg.drug_id] = clin_result

                if not pkg.invoke_procurement:
                    print(f"  [—] {pkg.drug_name} ({pkg.drug_id}) — monitor only")
                else:
                    status = "✓" if (proc_result or {}).get("parse_ok") else "✗"
                    print(f"\n  [{status}] {pkg.drug_name} ({pkg.drug_id}) complete")
                    if proc_result:
                        print_procurement_result(proc_result)

    # ── Aggregator ──
    review_packages = aggregate(
        packages     = packages,
        procurements = procurements,
        clinicals    = clinicals,
        event_type   = case["event_type"],
        severity     = case["severity"],
        db_path      = case["db"],
    )

    print(f"\n{'='*62}")
    print(f"  SUMMARY — {case['title']}")
    print(f"{'='*62}")
    print(f"  Total packages : {len(packages)}")
    print(f"  Actionable     : {len(actionable)}")
    print(f"  Written to DB  : {case['db']}")

    pending = get_pending_packages(case["db"])
    actionable = [rp for rp in pending if rp.get("action_required")]
    print(f"\n  Top actionable packages ({len(actionable)} total):")
    for rp in actionable[:3]:
        drug = rp.get("drug", {})
        proc = rp.get("procurement", {})
        print(f"\n    {rp['package_id']}")
        print(f"    Drug: {drug.get('drug_name', '?')} [{drug.get('overall_risk_level', '?')}]")
        print(f"    {rp.get('action_summary', '')}")
        print(f"    Viable: {proc.get('viable')} | Dicey: {proc.get('is_dicey_case')} | Gap: {proc.get('total_stock_gap', 'N/A')}")
        for h in rp.get("hospital_coverage", [])[:3]:
            print(f"      {h['hospital_id']} {h['hospital_name']:<25} "
                  f"need={h['units_required']:>5} get={h['units_acquired']:>5} ({h['coverage_status']})")

    print(f"{'='*62}\n")
    SESSION.reset()


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print_menu()
    case = pick_case()
    if case:
        run_pipeline(case)
