"""
sentinel.py

Entry point for the agent pipeline. Handles disruption event intake,
validation, and prediction engine invocation.

Responsibilities:
    1. Validate input — hard stops for invalid node type, severity, or
       node ID not found in Neo4j.
    2. Taxonomy check — soft warning if (node_type, event_type, severity)
       not in taxonomy. Falls back to severity defaults, does not block.
    3. Compound disruption detection — warns if this node is already
       marked offline in the current session.
    4. Node name resolution — fetches human-readable name from Neo4j
       for use in downstream agent prompts and review packages.
    5. Prediction engine invocation — calls run_prediction_pipeline,
       catches errors gracefully.
    6. Returns a DisruptionEvent dataclass carrying all event metadata
       and raw engine results for the Analyst.

What Sentinel does NOT do:
    - Group or filter results (Analyst)
    - Query distributor options (Procurement Agent)
    - Query alternative drugs (Clinical Agent)
    - Write to SQLite (Aggregator)
    - Call any LLM
"""

import pandas as pd
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional
from neo4j import GraphDatabase

from prediction_engine import (
    run_prediction_pipeline,
    get_recovery_days,
    DISRUPTION_TAXONOMY,
    SESSION,
)

# ── CONFIG ─────────────────────────────────────────────────────────────────────

NEO4J_URI      = "neo4j://127.0.0.1:7687"
NEO4J_USER     = "neo4j"
NEO4J_PASSWORD = "QWEasd123"

VALID_NODE_TYPES = {"Factory", "API", "Distributor"}
VALID_SEVERITIES = {"High", "Medium", "Low"}

driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

def _run(cypher, params=None):
    with driver.session() as s:
        return [dict(r) for r in s.run(cypher, params or {})]


# ── DISRUPTION EVENT DATACLASS ─────────────────────────────────────────────────

@dataclass
class DisruptionEvent:
    """
    Carries all event metadata and raw engine results.
    Passed to Analyst as the single input object.
    """
    # Input fields
    node_type:      str
    node_id:        str
    event_type:     str
    severity:       str
    triggered_date: str

    # Resolved by Sentinel
    node_name:       str
    recovery_days:   int
    taxonomy_match:  bool        # True if exact (node_type, event_type, severity) found

    # Engine results
    results:         List[dict]  # raw list from run_prediction_pipeline
    affected_count:  int         # len(results) — total (hospital, drug) pairs scored

    # Metadata
    processed_at: str
    warnings:     List[str] = field(default_factory=list)

    def summary(self):
        """Human-readable event summary for logging and dashboard header."""
        risk_counts = {}
        for r in self.results:
            tier = r.get("risk_level", "NO_RISK")
            risk_counts[tier] = risk_counts.get(tier, 0) + 1
        return (
            f"{self.node_type} {self.node_id} ({self.node_name}) | "
            f"{self.event_type} / {self.severity} | "
            f"Recovery ~{self.recovery_days}d | "
            f"Pairs: {self.affected_count} | "
            f"HIGH={risk_counts.get('HIGH_RISK',0)} "
            f"MED={risk_counts.get('MEDIUM_RISK',0)} "
            f"LOW={risk_counts.get('LOW_RISK',0)}"
        )


# ── VALIDATION ─────────────────────────────────────────────────────────────────

class SentinelError(Exception):
    """Raised for hard validation failures — pipeline stops."""
    pass


def _validate_node_type(node_type: str):
    if node_type not in VALID_NODE_TYPES:
        raise SentinelError(
            f"Invalid node_type '{node_type}'. "
            f"Must be one of: {sorted(VALID_NODE_TYPES)}"
        )


def _validate_severity(severity: str):
    if severity not in VALID_SEVERITIES:
        raise SentinelError(
            f"Invalid severity '{severity}'. "
            f"Must be one of: {sorted(VALID_SEVERITIES)}"
        )


def _validate_date(date_str: Optional[str]) -> str:
    """Validates date format, returns today's date if None."""
    if date_str is None:
        return datetime.now().strftime("%Y-%m-%d")
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
        return date_str
    except ValueError:
        raise SentinelError(
            f"Invalid date format '{date_str}'. Expected YYYY-MM-DD."
        )


def _resolve_node(node_type: str, node_id: str) -> str:
    """
    Confirms node exists in Neo4j and returns its name.
    Raises SentinelError if not found.
    """
    result = _run(
        f"MATCH (n:{node_type} {{id: $id}}) RETURN n.name AS name",
        {"id": node_id}
    )
    if not result:
        raise SentinelError(
            f"{node_type} '{node_id}' not found in Neo4j. "
            f"Check the node ID and ensure Neo4j is populated."
        )
    return result[0]["name"]


def _check_taxonomy(node_type: str, event_type: str, severity: str) -> tuple[bool, list]:
    """
    Checks if this exact combination exists in the taxonomy.
    Returns (match_found, warnings_list).
    Does not raise — mismatches are soft warnings.
    """
    warnings = []
    key = (node_type, event_type, severity)

    if key in DISRUPTION_TAXONOMY:
        return True, []

    # Check if node_type + event_type exists at any severity
    partial_match = any(
        k[0] == node_type and k[1] == event_type
        for k in DISRUPTION_TAXONOMY
    )

    if partial_match:
        warnings.append(
            f"Event type '{event_type}' exists for {node_type} but not "
            f"at severity '{severity}'. Using severity default for recovery days."
        )
    else:
        warnings.append(
            f"Event type '{event_type}' not found in taxonomy for {node_type}. "
            f"Using severity default for recovery days. "
            f"Valid events for {node_type}: "
            f"{sorted({k[1] for k in DISRUPTION_TAXONOMY if k[0]==node_type})}"
        )

    return False, warnings


def _check_compound_disruption(node_type: str, node_id: str) -> list:
    """
    Warns if this node is already offline in the current session.
    Only relevant for Factory nodes — session tracks factory status.
    """
    warnings = []
    if node_type == "Factory" and SESSION.is_factory_offline(node_id):
        warnings.append(
            f"Factory {node_id} is already marked offline in this session. "
            f"This is a compound disruption — scores will reflect accumulated impact."
        )
    return warnings


# ── MAIN SENTINEL FUNCTION ─────────────────────────────────────────────────────

def process_disruption(
    node_type:      str,
    node_id:        str,
    event_type:     str,
    severity:       str,
    triggered_date: Optional[str] = None,
    verbose:        bool = True,
) -> DisruptionEvent:
    """
    Main Sentinel entry point. Validates, resolves, and invokes the
    prediction engine. Returns a DisruptionEvent for the Analyst.

    Args:
        node_type:      "Factory" | "API" | "Distributor"
        node_id:        e.g. "F002", "A012", "S003"
        event_type:     must match taxonomy (e.g. "Disaster", "License Hold")
        severity:       "High" | "Medium" | "Low"
        triggered_date: "YYYY-MM-DD" — defaults to today if None
        verbose:        print progress to stdout

    Returns:
        DisruptionEvent with all metadata and engine results

    Raises:
        SentinelError: for hard validation failures (invalid input or
                       node not found in Neo4j)
    """
    processed_at = datetime.now().isoformat()
    all_warnings  = []

    if verbose:
        print(f"\n{'='*62}")
        print(f"  SENTINEL — Processing disruption event")
        print(f"{'='*62}")

    # ── Hard validations ───────────────────────────────────────────────────────
    _validate_node_type(node_type)
    if verbose: print(f"  node_type   : {node_type} ✓")

    _validate_severity(severity)
    if verbose: print(f"  severity    : {severity} ✓")

    triggered_date = _validate_date(triggered_date)
    if verbose: print(f"  date        : {triggered_date} ✓")

    node_name = _resolve_node(node_type, node_id)
    if verbose: print(f"  node        : {node_id} → {node_name} ✓")

    # ── Soft checks ────────────────────────────────────────────────────────────
    taxonomy_match, tax_warnings = _check_taxonomy(node_type, event_type, severity)
    all_warnings.extend(tax_warnings)

    compound_warnings = _check_compound_disruption(node_type, node_id)
    all_warnings.extend(compound_warnings)

    recovery_days = get_recovery_days(node_type, event_type, severity)
    if verbose:
        match_str = "taxonomy match" if taxonomy_match else "severity fallback"
        print(f"  event       : {event_type} / {severity} ({match_str})")
        print(f"  recovery    : ~{recovery_days} days")

    if all_warnings and verbose:
        print(f"\n  Warnings:")
        for w in all_warnings:
            print(f"    ⚠  {w}")

    # ── Engine invocation ──────────────────────────────────────────────────────
    if verbose:
        print(f"\n  Invoking prediction engine...")

    try:
        results = run_prediction_pipeline(
            node_type,
            node_id,
            event_type,
            severity,
            triggered_date,
        )
    except Exception as e:
        raise SentinelError(
            f"Prediction engine failed for {node_type} {node_id}: {e}"
        ) from e

    # ── Build and return DisruptionEvent ──────────────────────────────────────
    event = DisruptionEvent(
        node_type      = node_type,
        node_id        = node_id,
        event_type     = event_type,
        severity       = severity,
        triggered_date = triggered_date,
        node_name      = node_name,
        recovery_days  = recovery_days,
        taxonomy_match = taxonomy_match,
        results        = results,
        affected_count = len(results),
        processed_at   = processed_at,
        warnings       = all_warnings,
    )

    if verbose:
        print(f"\n  Event processed successfully.")
        print(f"  {event.summary()}")
        print(f"{'='*62}\n")

    return event


# ── TESTS ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":

    # Test 1: Valid event — Cipla Disaster High
    print("\nTEST 1: Valid — Cipla Disaster / High")
    event = process_disruption("Factory", "F002", "Disaster", "High", "2024-08-15")
    print(f"  Warnings: {event.warnings}")
    print(f"  HIGH_RISK pairs: {sum(1 for r in event.results if r['risk_level']=='HIGH_RISK')}")

    SESSION.reset()

    # Test 2: Valid — event not in taxonomy (soft warning)
    print("\nTEST 2: Event not in taxonomy — should warn but continue")
    event2 = process_disruption("Factory", "F002", "Flood", "High", "2024-08-15")
    print(f"  Warnings: {event2.warnings}")
    print(f"  Recovery days (severity fallback): {event2.recovery_days}")

    SESSION.reset()

    # Test 3: Hard fail — invalid node type
    print("\nTEST 3: Invalid node type — should raise SentinelError")
    try:
        process_disruption("Hospital", "H001", "Disaster", "High")
    except SentinelError as e:
        print(f"  SentinelError caught: {e}")

    # Test 4: Hard fail — node not in Neo4j
    print("\nTEST 4: Node not in Neo4j — should raise SentinelError")
    try:
        process_disruption("Factory", "F099", "Disaster", "High")
    except SentinelError as e:
        print(f"  SentinelError caught: {e}")

    # Test 5: Compound disruption warning
    print("\nTEST 5: Compound disruption — Cipla already offline")
    SESSION.set_factory_offline("F002")
    event5 = process_disruption("Factory", "F002", "Strike", "High", "2024-08-20")
    print(f"  Compound warning present: {'compound' in event5.warnings[0].lower() if event5.warnings else False}")

    SESSION.reset()

    # Test 6: API disruption
    print("\nTEST 6: Valid — A012 Supply Chain Failure / High")
    event6 = process_disruption("API", "A012", "Supply Chain Failure", "High", "2024-01-15")
    print(f"  Taxonomy match: {event6.taxonomy_match}")
    print(f"  Recovery days: {event6.recovery_days}")

    SESSION.reset()