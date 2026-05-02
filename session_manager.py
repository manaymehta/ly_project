"""
session_manager.py

Manages the simulation session state for the LY Project dashboard.

When a session is active, this module owns the single source of truth
for mutable simulation data:

  1. distributor_stock  — live stock per (distributor, hospital, drug)
                          Seeded from Neo4j DELIVERS_TO.currentStock on start.
                          Depleted when a procurement order is approved.

  2. hospital_inventory — live inventory per (hospital, drug)
                          Seeded from prediction_engine.SESSION.inventory on start.
                          Restocked when an approved order is received.

All reads during a session go through this module so the pipeline sees
depleted stock on subsequent disruptions within the same session.

Session lifecycle:
  start_session()     → seeds session.db from Neo4j + in-memory SESSION
  apply_depletion(…)  → deducts distributor stock, restocks hospital inventory
  get_distributor_stock(…) → read current stock for one (dist, hospital, drug)
  override_analyst_stock(rows) → patches currentStock in analyst query results
  end_session()       → drops session.db, resets SESSION
  is_active()         → bool — session.db exists and has an active session row
"""

import sqlite3
import os
from pathlib import Path
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────

PROJECT_DIR  = Path(__file__).parent
SESSION_DB   = str(PROJECT_DIR / "session.db")

NEO4J_URI      = "neo4j://127.0.0.1:7687"
NEO4J_USER     = "neo4j"
NEO4J_PASSWORD = "QWEasd123"


# ── Internal helpers ──────────────────────────────────────────────────────────

def _conn():
    conn = sqlite3.connect(SESSION_DB)
    conn.row_factory = sqlite3.Row
    return conn


def _neo4j_all_stock():
    """
    Pull every DELIVERS_TO relationship's stock/order/delivery data from Neo4j.
    Returns list of dicts with keys:
      distributor_id, hospital_id, drug_id,
      current_stock, min_order, delivery_days, price_per_unit
    """
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    with driver.session() as s:
        rows = s.run("""
            MATCH (d:Distributor)-[r:DELIVERS_TO]->(h:Hospital)
            RETURN d.id        AS distributor_id,
                   h.id        AS hospital_id,
                   r.drugId    AS drug_id,
                   r.currentStock  AS current_stock,
                   r.minOrder      AS min_order,
                   r.deliveryDays  AS delivery_days,
                   r.pricePerUnit  AS price_per_unit
        """)
        return [dict(r) for r in rows]
    driver.close()


# ── Schema ────────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS session_info (
    session_id   TEXT PRIMARY KEY,
    started_at   TEXT,
    is_active    INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS distributor_stock (
    distributor_id TEXT,
    hospital_id    TEXT,
    drug_id        TEXT,
    current_stock  REAL,
    baseline_stock REAL,
    min_order      REAL,
    delivery_days  REAL,
    price_per_unit REAL,
    PRIMARY KEY (distributor_id, hospital_id, drug_id)
);

CREATE TABLE IF NOT EXISTS hospital_inventory (
    hospital_id    TEXT,
    drug_id        TEXT,
    current_units  REAL,
    daily_demand   REAL,
    baseline_units REAL,
    PRIMARY KEY (hospital_id, drug_id)
);
"""


# ── Public API ────────────────────────────────────────────────────────────────

def is_active() -> bool:
    """Returns True if a session.db exists with an active session row."""
    if not os.path.exists(SESSION_DB):
        return False
    try:
        with _conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM session_info WHERE is_active = 1 LIMIT 1"
            ).fetchone()
            return row is not None
    except Exception:
        return False


def start_session() -> str:
    """
    Seeds session.db from Neo4j distributor stock + in-memory SESSION inventory.
    Returns the session_id.
    Raises RuntimeError if a session is already active.
    """
    if is_active():
        raise RuntimeError("A session is already active. Call end_session() first.")

    # Lazy import to avoid circular dependency at module level
    from prediction_engine import SESSION

    session_id = f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    # Build fresh DB
    if os.path.exists(SESSION_DB):
        os.remove(SESSION_DB)

    conn = sqlite3.connect(SESSION_DB)
    conn.executescript(_DDL)

    # ── 1. Session metadata ───────────────────────────────────────────────────
    conn.execute(
        "INSERT INTO session_info VALUES (?, ?, 1)",
        (session_id, datetime.now().isoformat())
    )

    # ── 2. Distributor stock from Neo4j ───────────────────────────────────────
    neo4j_rows = _neo4j_all_stock()
    conn.executemany("""
        INSERT OR REPLACE INTO distributor_stock
            (distributor_id, hospital_id, drug_id,
             current_stock, baseline_stock, min_order, delivery_days, price_per_unit)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        (r["distributor_id"], r["hospital_id"], r["drug_id"],
         r["current_stock"],  r["current_stock"],
         r["min_order"], r["delivery_days"], r["price_per_unit"])
        for r in neo4j_rows
    ])

    # ── 3. Hospital inventory from in-memory SESSION ───────────────────────────
    conn.executemany("""
        INSERT OR REPLACE INTO hospital_inventory
            (hospital_id, drug_id, current_units, daily_demand, baseline_units)
        VALUES (?, ?, ?, ?, ?)
    """, [
        (hosp_id, drug_id,
         inv["current_units"], inv["daily_demand"], inv["current_units"])
        for (hosp_id, drug_id), inv in SESSION.inventory.items()
    ])

    conn.commit()
    conn.close()

    print(f"[Session] Started: {session_id}")
    print(f"[Session] Seeded {len(neo4j_rows)} distributor-stock rows from Neo4j")
    print(f"[Session] Seeded {len(SESSION.inventory)} hospital-inventory rows")
    return session_id


def end_session():
    """
    Ends the active session: marks session inactive in DB, resets in-memory SESSION.
    Keeps session.db on disk for audit/history (does not delete it).
    """
    from prediction_engine import SESSION

    if os.path.exists(SESSION_DB):
        try:
            conn = sqlite3.connect(SESSION_DB)
            conn.execute("UPDATE session_info SET is_active = 0")
            conn.commit()
            conn.close()
        except Exception:
            pass

    SESSION.reset()
    print("[Session] Ended — base state restored.")


def get_distributor_stock(distributor_id: str, hospital_id: str, drug_id: str) -> float:
    """Returns current session stock for one (distributor, hospital, drug) triple."""
    if not is_active():
        raise RuntimeError("No active session.")
    with _conn() as conn:
        row = conn.execute("""
            SELECT current_stock FROM distributor_stock
            WHERE distributor_id = ? AND hospital_id = ? AND drug_id = ?
        """, (distributor_id, hospital_id, drug_id)).fetchone()
        return float(row["current_stock"]) if row else 0.0


def get_all_distributor_stock() -> dict:
    """
    Returns full stock snapshot as nested dict:
      { (distributor_id, hospital_id, drug_id): current_stock }
    """
    if not is_active():
        return {}
    with _conn() as conn:
        rows = conn.execute(
            "SELECT distributor_id, hospital_id, drug_id, current_stock FROM distributor_stock"
        ).fetchall()
        return {
            (r["distributor_id"], r["hospital_id"], r["drug_id"]): float(r["current_stock"])
            for r in rows
        }


def override_analyst_stock(distributor_rows: list) -> list:
    """
    Called by analyst._fetch_distributor_options() to replace Neo4j currentStock
    values with live session values when a session is active.

    distributor_rows: list of dicts returned by Neo4j (must include
        distributor_id, hospital_id (passed separately), drug_id, current_stock)
    Returns the same list with current_stock patched from session.db.
    """
    if not is_active():
        return distributor_rows

    stock_map = get_all_distributor_stock()
    for row in distributor_rows:
        key = (row.get("distributor_id"), row.get("hospital_id"), row.get("drug_id"))
        if key in stock_map:
            row["current_stock"] = stock_map[key]
    return distributor_rows


def apply_depletion(option: list, drug_id: str):
    """
    Applies an approved procurement option to the session state.

    option: list of allocation entries from procurement result's option_a or option_b.
    Each entry has:
        distributor_id, distributor_name,
        hospital_allocations: [{ hospital_id, units_allocated, ... }]

    What this does:
      - Deducts units_allocated from distributor_stock per (dist, hospital, drug)
      - Adds units_allocated to hospital_inventory.current_units per hospital
      - Updates in-memory SESSION.inventory to match
    """
    if not is_active():
        raise RuntimeError("No active session — cannot apply depletion.")

    from prediction_engine import SESSION

    conn = sqlite3.connect(SESSION_DB)

    for dist_entry in option:
        dist_id    = dist_entry.get("distributor_id")
        allocations = dist_entry.get("hospital_allocations") or []

        for alloc in allocations:
            hosp_id = alloc.get("hospital_id")
            units   = int(alloc.get("units_allocated") or 0)
            if not hosp_id or units <= 0:
                continue

            # ── Deduct distributor stock ──────────────────────────────────────
            conn.execute("""
                UPDATE distributor_stock
                SET current_stock = MAX(0, current_stock - ?)
                WHERE distributor_id = ? AND hospital_id = ? AND drug_id = ?
            """, (units, dist_id, hosp_id, drug_id))

            # ── Restock hospital inventory ────────────────────────────────────
            conn.execute("""
                UPDATE hospital_inventory
                SET current_units = current_units + ?
                WHERE hospital_id = ? AND drug_id = ?
            """, (units, hosp_id, drug_id))

            # ── Sync in-memory SESSION ────────────────────────────────────────
            SESSION.restock_inventory(hosp_id, drug_id, units)

    conn.commit()
    conn.close()
    print(f"[Session] Depletion applied for drug {drug_id}.")


def get_session_state() -> dict:
    """
    Returns a full snapshot of the current session state for the dashboard.
    Includes deltas (current vs baseline) for each distributor and hospital.
    """
    if not is_active():
        return {"active": False}

    with _conn() as conn:
        info = conn.execute(
            "SELECT * FROM session_info WHERE is_active = 1"
        ).fetchone()

        dist_rows = conn.execute("""
            SELECT distributor_id, hospital_id, drug_id,
                   current_stock, baseline_stock,
                   (baseline_stock - current_stock) AS depleted
            FROM distributor_stock
            WHERE depleted > 0
            ORDER BY depleted DESC
        """).fetchall()

        hosp_rows = conn.execute("""
            SELECT hospital_id, drug_id,
                   current_units, baseline_units,
                   (current_units - baseline_units) AS restocked
            FROM hospital_inventory
            WHERE restocked > 0
            ORDER BY restocked DESC
        """).fetchall()

    return {
        "active":      True,
        "session_id":  info["session_id"] if info else None,
        "started_at":  info["started_at"] if info else None,
        "depletions":  [dict(r) for r in dist_rows],
        "restocks":    [dict(r) for r in hosp_rows],
    }
