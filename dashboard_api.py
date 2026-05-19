"""
dashboard_api.py  —  LY Project Dashboard Backend (FastAPI)

Run:
    conda run -n ml_env uvicorn dashboard_api:app --reload --port 8000
Then open:
    http://localhost:8000
"""

import glob
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Add core/ to path so pipeline modules are importable
sys.path.insert(0, str(Path(__file__).parent / "core"))
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ── App ───────────────────────────────────────────────────────────────────────

PROJECT_DIR = Path(__file__).parent
REVIEWS_DB  = str(PROJECT_DIR / "db" / "reviews.db")   # permanent audit log

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    # reviews.db is a permanent audit log — initialize on every server start
    # so it always exists even before the first disruption is run
    from aggregator import init_db
    init_db(REVIEWS_DB)
    print(f"[Startup] reviews.db ready at {REVIEWS_DB}")
    yield

app = FastAPI(title="LY Project Dashboard", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_db_files():
    return sorted(glob.glob(str(PROJECT_DIR / "*.db")),
                  key=os.path.getmtime, reverse=True)


def resolve_db(db_name: Optional[str] = None) -> str:
    if db_name:
        p = PROJECT_DIR / db_name
        if p.exists():
            return str(p)
    # Default to reviews.db if it exists, else latest
    if os.path.exists(REVIEWS_DB):
        return REVIEWS_DB
    files = get_db_files()
    if files:
        return files[0]
    raise HTTPException(status_code=404, detail="No database found")


def query(db_path: str, sql: str, params: tuple = ()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Static files ──────────────────────────────────────────────────────────────

FRONTEND_DIST = PROJECT_DIR / "frontend" / "dist"

# Mount compiled React assets (js/css chunks) if the build exists
if FRONTEND_DIST.exists():
    app.mount(
        "/assets",
        StaticFiles(directory=str(FRONTEND_DIST / "assets")),
        name="static-assets",
    )

@app.get("/", response_class=FileResponse)
def index():
    html = FRONTEND_DIST / "index.html"
    if html.exists():
        return FileResponse(str(html))
    raise HTTPException(
        status_code=404,
        detail="Frontend not built. Run: cd frontend && npm run build",
    )

# ── /api/db-list ──────────────────────────────────────────────────────────────

@app.get("/api/db-list")
def db_list():
    return [os.path.basename(f) for f in get_db_files()]


# ── /api/stats ────────────────────────────────────────────────────────────────

@app.get("/api/stats")
def stats(db: Optional[str] = Query(None), since: Optional[str] = Query(None)):
    db_path = resolve_db(db)
    if since:
        rows = query(db_path,
            "SELECT overall_risk_level, status, full_package FROM review_packages "
            "WHERE created_at >= ?", (since,))
    else:
        rows = query(db_path,
            "SELECT overall_risk_level, status, full_package FROM review_packages")

    total     = len(rows)
    high      = sum(1 for r in rows if r["overall_risk_level"] == "HIGH_RISK")
    medium    = sum(1 for r in rows if r["overall_risk_level"] == "MEDIUM_RISK")
    pending   = sum(1 for r in rows if r["status"] == "pending_review")
    dicey     = 0
    zero_hosp = 0

    for r in rows:
        pkg  = json.loads(r["full_package"])
        proc = pkg.get("procurement") or {}
        if proc.get("is_dicey_case"):
            dicey += 1
        for h in pkg.get("hospital_coverage") or []:
            if h.get("coverage_status") == "ZERO":
                zero_hosp += 1

    event = {}
    if since:
        meta = query(db_path,
            "SELECT disruption_node, disruption_event, disruption_severity "
            "FROM review_packages WHERE created_at >= ? ORDER BY created_at DESC LIMIT 1",
            (since,))
    else:
        meta = query(db_path,
            "SELECT disruption_node, disruption_event, disruption_severity "
            "FROM review_packages ORDER BY created_at DESC LIMIT 1")
    if meta:
        event = meta[0]

    return {"total": total, "high_risk": high, "medium_risk": medium,
            "pending": pending, "dicey": dicey, "zero_hospitals": zero_hosp,
            **event}


# ── /api/packages ─────────────────────────────────────────────────────────────

RISK_ORDER = {"HIGH_RISK": 1, "MEDIUM_RISK": 2, "LOW_RISK": 3, "NO_RISK": 4}


@app.get("/api/packages")
def packages(db: Optional[str] = Query(None), since: Optional[str] = Query(None)):
    db_path = resolve_db(db)
    if since:
        rows = query(db_path, """
            SELECT package_id, disruption_node, disruption_event, disruption_severity,
                   drug_id, drug_name, criticality, overall_risk_level,
                   procurement_viable, clinical_suppressed, substitution_viable,
                   status, created_at, full_package
            FROM review_packages
            WHERE created_at >= ?
        """, (since,))
    else:
        rows = query(db_path, """
            SELECT package_id, disruption_node, disruption_event, disruption_severity,
                   drug_id, drug_name, criticality, overall_risk_level,
                   procurement_viable, clinical_suppressed, substitution_viable,
                   status, created_at, full_package
            FROM review_packages
        """)

    result = []
    for r in rows:
        pkg      = json.loads(r["full_package"])
        coverage = pkg.get("hospital_coverage") or []
        proc     = pkg.get("procurement") or {}
        clin     = pkg.get("clinical") or {}

        result.append({
            "package_id":          r["package_id"],
            "drug_id":             r["drug_id"],
            "drug_name":           r["drug_name"],
            "risk_level":          r["overall_risk_level"],
            "status":              r["status"],
            "procurement_viable":  bool(r["procurement_viable"]),
            "clinical_suppressed": bool(r["clinical_suppressed"]),
            "substitution_viable": r["substitution_viable"],
            "is_dicey":            bool(proc.get("is_dicey_case")),
            "action_required":     bool(pkg.get("action_required")),
            "action_summary":      pkg.get("action_summary", ""),
            "total_stock_gap":     proc.get("total_stock_gap", 0),
            "coverage": {
                "full":    sum(1 for h in coverage if h.get("coverage_status") == "ALLOCATED"),
                "partial": sum(1 for h in coverage if h.get("coverage_status") == "PARTIAL"),
                "zero":    sum(1 for h in coverage if h.get("coverage_status") == "ZERO"),
            },
            "total_hospitals":     len(coverage),
            "max_shortage_days":   max((h.get("coverage_gap") or 0 for h in coverage), default=0),
            "affected_hospitals":  sum(1 for h in coverage if (h.get("coverage_gap") or 0) > 0),
            "disruption_node":     r["disruption_node"],
            "disruption_event":    r["disruption_event"],
            "disruption_severity": r["disruption_severity"],
            "created_at":          r["created_at"],
            "substitution_name":   clin.get("recommended_alt_name"),
            "physician_signoff":   clin.get("requires_physician_approval", False),
        })

    result.sort(key=lambda x: RISK_ORDER.get(x["risk_level"], 9))
    return result


# ── /api/packages/{id}/outcome ───────────────────────────────────────────────
# MUST be registered BEFORE /api/packages/{package_id:path} — the :path wildcard
# would otherwise swallow the /outcome suffix and shadow this endpoint entirely.

@app.get("/api/packages/{package_id:path}/outcome")
def package_outcome(
    package_id: str,
    action: str = Query("approve_a"),
    db: Optional[str] = Query(None),
):
    """
    Runs the stock trajectory simulation for a package + decision.

    action: "approve_a" | "approve_b" | "reject"
      approve_a / approve_b — uses that option's delivery schedule as the approved trajectory
      reject                — approved trajectory uses option_a as the counterfactual
                              (shows what approving would have done vs what rejection means)

    Returns approved + rejected trajectories per gap hospital plus summary stats.
    """
    db_path = resolve_db(db)
    rows = query(db_path,
        "SELECT full_package FROM review_packages WHERE package_id = ?", (package_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="Package not found")

    pkg           = json.loads(rows[0]["full_package"])
    procurement   = pkg.get("procurement", {})
    coverage      = pkg.get("hospital_coverage", [])
    recovery_days = int((pkg.get("disruption") or {}).get("recovery_days") or 30)

    option_key = "option_b" if action == "approve_b" else "option_a"

    from outcome_simulator import simulate
    return simulate(coverage, procurement, recovery_days, option_key)


# ── /api/packages/{id} ────────────────────────────────────────────────────────

@app.get("/api/packages/{package_id:path}")
def package_detail(package_id: str, db: Optional[str] = Query(None)):
    db_path = resolve_db(db)
    rows = query(db_path,
        "SELECT * FROM review_packages WHERE package_id = ?", (package_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="Package not found")

    r   = rows[0]
    pkg = json.loads(r["full_package"])
    return {
        **{k: v for k, v in r.items() if k != "full_package"},
        "recovery_days":      (pkg.get("disruption") or {}).get("recovery_days"),
        "procurement_action": r.get("procurement_action"),   # JSON string or None
        "drug":              pkg.get("drug", {}),
        "hospital_coverage": pkg.get("hospital_coverage", []),
        "procurement":       pkg.get("procurement", {}),
        "clinical":          pkg.get("clinical", {}),
        "action_summary":    pkg.get("action_summary", ""),
        "action_required":   pkg.get("action_required", False),
    }


# ── /api/heatmap ──────────────────────────────────────────────────────────────

@app.get("/api/heatmap")
def heatmap(db: Optional[str] = Query(None)):
    db_path = resolve_db(db)
    rows = query(db_path,
        "SELECT drug_id, drug_name, overall_risk_level, full_package "
        "FROM review_packages")

    hospitals, drugs, cells = {}, [], {}
    for r in rows:
        pkg = json.loads(r["full_package"])
        drugs.append({"id": r["drug_id"], "name": r["drug_name"],
                       "risk": r["overall_risk_level"]})
        for h in (pkg.get("hospital_coverage") or []):
            hid = h.get("hospital_id")
            if hid:
                hospitals[hid] = h.get("hospital_name", hid)
                cells[f"{hid}__{r['drug_id']}"] = {
                    "status":   h.get("coverage_status", "NONE"),
                    "required": h.get("units_required", 0),
                    "acquired": h.get("units_acquired", 0),
                }
    return {
        "hospitals": [{"id": k, "name": v} for k, v in sorted(hospitals.items())],
        "drugs":     drugs,
        "cells":     cells,
    }


# ── /api/distributors ─────────────────────────────────────────────────────────

@app.get("/api/distributors")
def distributors(db: Optional[str] = Query(None)):
    db_path = resolve_db(db)
    rows = query(db_path, "SELECT drug_name, full_package FROM review_packages")

    dist = {}
    for r in rows:
        pkg    = json.loads(r["full_package"])
        option = (pkg.get("procurement") or {}).get("option_a") or []
        for entry in option:
            did = entry.get("distributor_id")
            if not did:
                continue
            if did not in dist:
                dist[did] = {"id": did, "name": entry.get("distributor_name", did),
                             "total_assigned": 0, "below_min": 0,
                             "drugs": set(), "hospitals": set()}
            dist[did]["total_assigned"] += int(entry.get("total_quantity") or 0)
            dist[did]["drugs"].add(r["drug_name"])
            for alloc in (entry.get("hospital_allocations") or []):
                dist[did]["hospitals"].add(alloc.get("hospital_id", ""))
            if "BELOW MIN" in (entry.get("distributor_caveat") or ""):
                dist[did]["below_min"] += 1

    result = []
    for d in dist.values():
        result.append({**{k: v for k, v in d.items() if k not in ("drugs", "hospitals")},
                       "drug_count": len(d["drugs"]),
                       "hospital_count": len(d["hospitals"])})
    result.sort(key=lambda x: -x["total_assigned"])
    return result


# ── /api/packages/{id}/action ─────────────────────────────────────────────────

class ActionRequest(BaseModel):
    action: str  # "approve_a" | "approve_b" | "reject"


@app.post("/api/packages/{package_id:path}/action")
def package_action(package_id: str, body: ActionRequest,
                   db: Optional[str] = Query(None)):
    db_path = resolve_db(db)

    if body.action in ("approve_a", "approve_b"):
        status      = "approved"
        proc_action = json.dumps({"approved_order":
                                  "option_a" if body.action == "approve_a" else "option_b"})
    elif body.action == "reject":
        status, proc_action = "rejected", json.dumps({})
    else:
        raise HTTPException(status_code=400, detail="Invalid action")

    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE review_packages SET status=?, procurement_action=?, "
        "resolved_at=datetime('now') WHERE package_id=?",
        (status, proc_action, package_id)
    )
    conn.commit()
    conn.close()

    # If approving, apply depletion to session stock
    if body.action in ("approve_a", "approve_b"):
        try:
            import session_manager
            if session_manager.is_active():
                rows = query(db_path,
                    "SELECT drug_id, full_package FROM review_packages "
                    "WHERE package_id = ?", (package_id,))
                if rows:
                    pkg    = json.loads(rows[0]["full_package"])
                    drug_id = rows[0]["drug_id"]
                    option_key = "option_a" if body.action == "approve_a" else "option_b"
                    option = (pkg.get("procurement") or {}).get(option_key) or []
                    session_manager.apply_depletion(option, drug_id)
        except Exception as e:
            print(f"[WARN] Depletion failed: {e}")

    return {"success": True, "status": status}


# ── /api/packages/{id}/retry-procurement ─────────────────────────────────────

@app.post("/api/packages/{package_id:path}/retry-procurement")
def retry_procurement(package_id: str, db: Optional[str] = Query(None)):
    """
    Re-runs the procurement agent for a package that previously failed with an API error.
    Rebuilds the DrugAlertPackage from stored disruption params, runs the LLM call,
    and updates the existing DB record in place.
    """
    db_path = resolve_db(db)
    rows = query(db_path,
        "SELECT drug_id, disruption_node, disruption_event, disruption_severity, "
        "full_package FROM review_packages WHERE package_id = ?", (package_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="Package not found")

    r           = rows[0]
    node_id     = r["disruption_node"]
    event_type  = r["disruption_event"]
    severity    = r["disruption_severity"]
    full_pkg    = json.loads(r["full_package"])

    # Infer node_type from stored full_package disruption block
    disruption    = full_pkg.get("disruption") or {}
    triggered_date = disruption.get("triggered_date") or datetime.now().strftime("%Y-%m-%d")

    # node_type: derive from ID prefix (F→Factory, S→Distributor, A→API)
    prefix_map = {"F": "Factory", "S": "Distributor", "A": "API"}
    node_type  = prefix_map.get((node_id or "")[:1], "Factory")

    try:
        from sentinel           import process_disruption
        from analyst            import analyse
        from procurement_agent  import run_procurement_agent
        from aggregator         import _build_package_row   # type: ignore

        event    = process_disruption(node_type, node_id, event_type, severity, triggered_date)
        packages = analyse(event, verbose=False)
        pkg_obj  = next((p for p in packages if p.drug_id == r["drug_id"]), None)

        if pkg_obj is None:
            raise HTTPException(status_code=422,
                detail=f"Drug {r['drug_id']} not found in re-analysis results.")

        proc = run_procurement_agent(pkg_obj, verbose=True)

        if proc.get("api_error"):
            raise HTTPException(status_code=503,
                detail="LLM API still returning errors — try again in a moment.")

        # Re-aggregate just this one drug and overwrite its DB row
        from aggregator import aggregate
        aggregate(
            packages     = [pkg_obj],
            procurements = {r["drug_id"]: proc},
            clinicals    = {},
            event_type   = event_type,
            severity     = severity,
            db_path      = db_path,
        )

        return {"success": True, "drug_id": r["drug_id"]}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Retry failed: {e}")


# ── /api/graph/nodes ──────────────────────────────────────────────────────────

NEO4J_URI      = os.environ["NEO4J_URI"]
NEO4J_USER     = os.environ["NEO4J_USER"]
NEO4J_PASSWORD = os.environ["NEO4J_PASSWORD"]

# vis.js group → colour mapping (used by frontend for node colouring)
NODE_COLOURS = {
    "Factory":     "#e07b54",
    "Drug":        "#6c9dc6",
    "Distributor": "#82c091",
    "Hospital":    "#b39ddb",
    "API":         "#f0c040",
}

@app.get("/api/graph/nodes")
def graph_nodes():
    """
    Returns the full supply chain graph in vis.js-compatible format:
      { nodes: [...], edges: [...] }

    Main chain: Factory → Drug → Distributor → Hospital
    Side nodes: API (connected to Factory via PRODUCES_API and to Drug via COMPONENT_OF)

    Each node includes:
      id, label, group (node type), title (tooltip), disruptable (bool)

    Each edge includes:
      from, to, label (relationship type)

    Disruptable nodes (Factory, Distributor, API) show the Disrupt action in the UI.
    Display-only nodes (Drug, Hospital) do not.
    """
    try:
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

        nodes_map = {}   # id → node dict  (dedup)
        edges     = []

        with driver.session() as s:
            # ── Factories ──────────────────────────────────────────────────────
            for r in s.run("MATCH (n:Factory) RETURN n.id AS id, n.name AS name, n.city AS city"):
                nodes_map[r["id"]] = {
                    "id":         r["id"],
                    "label":      r["id"],
                    "title":      f"{r['name']} ({r['city']})",
                    "group":      "Factory",
                    "disruptable": True,
                    "color":      NODE_COLOURS["Factory"],
                }

            # ── Drugs ──────────────────────────────────────────────────────────
            for r in s.run("MATCH (n:Drug) RETURN n.id AS id, n.name AS name, n.criticality AS crit"):
                nodes_map[r["id"]] = {
                    "id":         r["id"],
                    "label":      r["id"],
                    "title":      f"{r['name']} [{r['crit']}]",
                    "group":      "Drug",
                    "disruptable": False,
                    "color":      NODE_COLOURS["Drug"],
                }

            # ── Distributors ───────────────────────────────────────────────────
            for r in s.run("MATCH (n:Distributor) RETURN n.id AS id, n.name AS name, n.city AS city"):
                nodes_map[r["id"]] = {
                    "id":         r["id"],
                    "label":      r["id"],
                    "title":      f"{r['name']} ({r['city']})",
                    "group":      "Distributor",
                    "disruptable": True,
                    "color":      NODE_COLOURS["Distributor"],
                }

            # ── Hospitals ──────────────────────────────────────────────────────
            for r in s.run("MATCH (n:Hospital) RETURN n.id AS id, n.name AS name, n.city AS city"):
                nodes_map[r["id"]] = {
                    "id":         r["id"],
                    "label":      r["id"],
                    "title":      f"{r['name']} ({r['city']})",
                    "group":      "Hospital",
                    "disruptable": False,
                    "color":      NODE_COLOURS["Hospital"],
                }

            # ── APIs ───────────────────────────────────────────────────────────
            for r in s.run("MATCH (n:API) RETURN n.id AS id, n.name AS name"):
                nodes_map[r["id"]] = {
                    "id":         r["id"],
                    "label":      r["id"],
                    "title":      f"API: {r['name']}",
                    "group":      "API",
                    "disruptable": True,
                    "color":      NODE_COLOURS["API"],
                }

            # ── Edges: Factory → API ───────────────────────────────────────────
            for r in s.run("MATCH (f:Factory)-[:PRODUCES_API]->(a:API) RETURN f.id AS f, a.id AS a"):
                edges.append({"from": r["f"], "to": r["a"], "label": "PRODUCES_API"})

            # ── Edges: API → Drug ──────────────────────────────────────────────
            for r in s.run("MATCH (a:API)-[:COMPONENT_OF]->(d:Drug) RETURN a.id AS a, d.id AS d"):
                edges.append({"from": r["a"], "to": r["d"], "label": "COMPONENT_OF"})

            # ── Edges: Factory → Drug (synthesised via API) ────────────────────
            # No direct Neo4j relationship — traverse Factory→API→Drug
            seen_fd = set()
            for r in s.run("""
                MATCH (f:Factory)-[:PRODUCES_API]->(a:API)-[:COMPONENT_OF]->(d:Drug)
                RETURN DISTINCT f.id AS f, d.id AS d
            """):
                key = (r["f"], r["d"])
                if key not in seen_fd:
                    seen_fd.add(key)
                    edges.append({"from": r["f"], "to": r["d"], "label": "MANUFACTURES"})

            # ── Edges: Drug → Distributor ──────────────────────────────────────
            # DELIVERS_TO is Distributor→Hospital with a drugId property.
            # Synthesise one Drug→Distributor edge per unique (drug, distributor) pair
            # to complete the main supply chain: Factory→API→Drug→Distributor→Hospital
            seen_dd = set()
            for r in s.run("""
                MATCH (dist:Distributor)-[rel:DELIVERS_TO]->(h:Hospital)
                WHERE rel.drugId IS NOT NULL
                RETURN DISTINCT rel.drugId AS drug_id, dist.id AS dist_id
            """):
                key = (r["drug_id"], r["dist_id"])
                if key not in seen_dd:
                    seen_dd.add(key)
                    edges.append({"from": r["drug_id"], "to": r["dist_id"], "label": "SUPPLIED_BY"})

            # ── Edges: Distributor → Hospital ──────────────────────────────────
            # Deduplicate: one edge per (distributor, hospital) pair regardless of drug
            seen_dh = set()
            for r in s.run("""
                MATCH (s:Distributor)-[:DELIVERS_TO]->(h:Hospital)
                RETURN DISTINCT s.id AS s, h.id AS h
            """):
                key = (r["s"], r["h"])
                if key not in seen_dh:
                    seen_dh.add(key)
                    edges.append({"from": r["s"], "to": r["h"], "label": "DELIVERS_TO"})

        driver.close()

        return {
            "nodes": list(nodes_map.values()),
            "edges": edges,
            "legend": NODE_COLOURS,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Neo4j error: {e}")


# ── /api/graph/vulnerability ──────────────────────────────────────────────────

@app.get("/api/graph/vulnerability")
def graph_vulnerability():
    """
    Same topology as /api/graph/nodes but each node carries GNN scores:
      vulnerability_score, centrality_score, dependency_score
    Scores are written to Neo4j by gnn_centrality.py.
    """
    try:
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

        nodes_map = {}
        edges     = []

        with driver.session() as s:
            # ── Nodes with GNN scores ─────────────────────────────────────────
            for label, disruptable in [
                ("Factory",     True),
                ("Drug",        False),
                ("Distributor", True),
                ("Hospital",    False),
                ("API",         True),
            ]:
                for r in s.run(f"""
                    MATCH (n:{label})
                    RETURN n.id AS id, n.name AS name,
                           coalesce(n.vulnerabilityScore, 0.0) AS vs,
                           coalesce(n.centralityScore,    0.0) AS cs,
                           coalesce(n.dependencyScore,    0.0) AS ds
                """):
                    nodes_map[r["id"]] = {
                        "id":                r["id"],
                        "label":             r["id"],
                        "title":             f"{r['name']} | vuln={r['vs']:.3f} cent={r['cs']:.3f} dep={r['ds']:.3f}",
                        "group":             label,
                        "disruptable":       disruptable,
                        "vulnerability_score": round(float(r["vs"]), 4),
                        "centrality_score":    round(float(r["cs"]), 4),
                        "dependency_score":    round(float(r["ds"]), 4),
                    }

            # ── Edges (same as supply chain graph) ────────────────────────────
            for r in s.run("MATCH (f:Factory)-[:PRODUCES_API]->(a:API) RETURN f.id AS f, a.id AS a"):
                edges.append({"from": r["f"], "to": r["a"], "label": "PRODUCES_API"})

            for r in s.run("MATCH (a:API)-[:COMPONENT_OF]->(d:Drug) RETURN a.id AS a, d.id AS d"):
                edges.append({"from": r["a"], "to": r["d"], "label": "COMPONENT_OF"})

            seen_fd = set()
            for r in s.run("""
                MATCH (f:Factory)-[:PRODUCES_API]->(a:API)-[:COMPONENT_OF]->(d:Drug)
                RETURN DISTINCT f.id AS f, d.id AS d
            """):
                key = (r["f"], r["d"])
                if key not in seen_fd:
                    seen_fd.add(key)
                    edges.append({"from": r["f"], "to": r["d"], "label": "MANUFACTURES"})

            seen_dd = set()
            for r in s.run("""
                MATCH (dist:Distributor)-[rel:DELIVERS_TO]->(h:Hospital)
                WHERE rel.drugId IS NOT NULL
                RETURN DISTINCT rel.drugId AS drug_id, dist.id AS dist_id
            """):
                key = (r["drug_id"], r["dist_id"])
                if key not in seen_dd:
                    seen_dd.add(key)
                    edges.append({"from": r["drug_id"], "to": r["dist_id"], "label": "SUPPLIED_BY"})

            seen_dh = set()
            for r in s.run("MATCH (s:Distributor)-[:DELIVERS_TO]->(h:Hospital) RETURN DISTINCT s.id AS s, h.id AS h"):
                key = (r["s"], r["h"])
                if key not in seen_dh:
                    seen_dh.add(key)
                    edges.append({"from": r["s"], "to": r["h"], "label": "DELIVERS_TO"})

        driver.close()
        return {"nodes": list(nodes_map.values()), "edges": edges}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Neo4j error: {e}")


# ── Session endpoints ─────────────────────────────────────────────────────────

@app.post("/api/session/start")
def session_start():
    import session_manager
    try:
        sid = session_manager.start_session()
        return {"success": True, "session_id": sid}
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Session start failed: {e}")


@app.post("/api/session/end")
def session_end():
    import session_manager
    session_manager.end_session()
    return {"success": True}


@app.get("/api/session/state")
def session_state():
    import session_manager
    return session_manager.get_session_state()


class DisruptionRequest(BaseModel):
    node_type:  str           # "Factory" | "Distributor" | "API"
    node_id:    str           # e.g. "F002", "S003", "A012"
    event_type: str           # e.g. "Disaster", "Strike", "Logistics Failure"
    severity:   str           # "High" | "Medium" | "Low"
    month:      int           # 1–12
    day:        int           # 1–31


# Valid (node_type, event_type) pairs — derived from data/disruption_taxonomy.csv.
# Used as a server-side safety net to reject invalid combinations even if the
# frontend is bypassed.
VALID_EVENT_TYPES: dict[str, list[str]] = {
    "Factory":     ["Disaster", "Equipment Failure", "Strike", "License Hold", "Raw Material Shortage"],
    "Distributor": ["Logistics Failure", "Strike", "License Suspension", "Storage Failure", "Disaster"],
    "API":         ["Raw Material Shortage", "Supply Chain Failure"],
}


@app.post("/api/session/run-disruption")
def run_disruption(body: DisruptionRequest):
    """
    Triggers the full pipeline for a disruption event.
    Year is auto-set to current year — Prophet only needs month+day for seasonality.
    Results are written to reviews.db.
    """
    import session_manager
    if not session_manager.is_active():
        raise HTTPException(status_code=400,
                            detail="No active session. Call /api/session/start first.")

    # ── Taxonomy validation (server-side safety net) ───────────────────────────
    valid_events = VALID_EVENT_TYPES.get(body.node_type)
    if valid_events is None:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid node_type '{body.node_type}'. "
                   f"Must be one of: {sorted(VALID_EVENT_TYPES.keys())}",
        )
    if body.event_type not in valid_events:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Event type '{body.event_type}' is not valid for node type '{body.node_type}'. "
                f"Valid event types: {', '.join(valid_events)}"
            ),
        )

    year = datetime.now().year
    triggered_date = f"{year}-{body.month:02d}-{body.day:02d}"

    try:
        from sentinel  import process_disruption
        from analyst   import analyse
        from aggregator import aggregate
        from procurement_agent import run_procurement_agent
        from clinical_agent    import run_clinical_agent

        event = process_disruption(
            node_type      = body.node_type,
            node_id        = body.node_id,
            event_type     = body.event_type,
            severity       = body.severity,
            triggered_date = triggered_date,
        )

        packages = analyse(event)

        procurements, clinicals = {}, {}
        from concurrent.futures import ThreadPoolExecutor, wait, ALL_COMPLETED

        actionable = [p for p in packages if p.invoke_procurement or p.invoke_clinical]

        def _run_drug(pkg):
            proc = run_procurement_agent(pkg) if pkg.invoke_procurement else None
            clin = run_clinical_agent(pkg)    if pkg.invoke_clinical    else None
            return pkg.drug_id, proc, clin

        BATCH_SIZE = 2
        batches = [actionable[i:i+BATCH_SIZE] for i in range(0, len(actionable), BATCH_SIZE)]

        with ThreadPoolExecutor(max_workers=BATCH_SIZE) as ex:
            for b_idx, batch in enumerate(batches):
                print(f"[Pipeline] Batch {b_idx+1}/{len(batches)}: {[p.drug_id for p in batch]}")
                futures = {ex.submit(_run_drug, pkg): pkg.drug_id for pkg in batch}

                # Block until every drug in this batch has finished both LLM calls
                # before submitting the next batch — guarantees max 2 concurrent calls
                done, _ = wait(futures, return_when=ALL_COMPLETED)

                for fut in done:
                    try:
                        drug_id, proc, clin = fut.result()
                        if proc is not None:
                            procurements[drug_id] = proc
                        if clin is not None:
                            clinicals[drug_id] = clin
                    except Exception as e:
                        print(f"[✗] Drug failed: {e}")

        aggregate(
            packages     = packages,
            procurements = procurements,
            clinicals    = clinicals,
            event_type   = body.event_type,
            severity     = body.severity,
            db_path      = REVIEWS_DB,
        )

        return {
            "success":           True,
            "triggered_date":    triggered_date,
            "total_packages":    len(packages),
            "actionable":        len(actionable),
            "affected_drug_ids": [p.drug_id for p in actionable],
            "db":            os.path.basename(REVIEWS_DB),
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── SPA fallback ─────────────────────────────────────────────────────────────
# Must be LAST — after all /api/* routes so it never shadows them.
# Any unmatched path (e.g. /some/react/route) returns index.html.

@app.get("/{full_path:path}", response_class=FileResponse)
def spa_fallback(full_path: str):
    html = FRONTEND_DIST / "index.html"
    if html.exists():
        return FileResponse(str(html))
    raise HTTPException(status_code=404, detail="Frontend not built")


# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    print("\n  LY Project Dashboard API (FastAPI)")
    print("  Docs:      http://localhost:8000/docs")
    print("  Dashboard: http://localhost:8000\n")
    uvicorn.run("dashboard_api:app", host="0.0.0.0", port=8000, reload=True)
