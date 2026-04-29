"""
gnn_centrality.py

GNN Component 1 — Structural Vulnerability Analysis via Neo4j GDS.

Two complementary scores per node:

    centralityScore  [0-1]  Betweenness centrality, per-label normalised.
                            How many supply chain paths route through this node.

    dependencyScore  [0-1]  Custom irreplaceability score.
                            How much damage occurs if this node fails.
                            Computed differently per node type — see below.

    vulnerabilityScore [0-1] = 0.2 × centralityScore + 0.8 × dependencyScore
                            Dependency weighted higher: sole-source risk is
                            categorically more dangerous than high connectivity.

Dependency formula per node type:

    Factory:
        max(max_capacity_share, num_apis_produced / total_apis)
        Captures both DEPTH (how dominant on any one API) and
        BREADTH (how many APIs are simultaneously affected if lost).
        F005 Lupin: max(0.29, 4/12) = 0.333 — partial but broad impact.
        F001 Biocon: max(1.0, 1/12) = 1.0 — sole Insulin producer.

    API:
        base_dep × max_downstream_criticality_weight
        base_dep = 1.0 if sole producer, else 1 - min_capacity_share
        criticality_weights = {Life-Critical:1.0, High:0.75, Moderate:0.50, Low:0.25}
        Breaks the 1.0 cluster: A012→Life-Critical drugs=1.0, A007→Low drugs=0.5
        A sole-source API for a Low criticality drug is less dangerous
        than a sole-source API for a Life-Critical drug.

    Drug:
        max dependency score of its source APIs.
        A drug is as vulnerable as its most vulnerable API.
        Inherits criticality-adjusted API scores automatically.

    Distributor:
        1 - (drugs_carried / total_drugs)
        Specialist distributor = high dependency (fewer alternatives exist).
        General distributor carrying all drugs = low dependency (replaceable).
        S007 BioSupply (1 drug): 1 - 1/20 = 0.95
        S001 MedPlus (20 drugs): 1 - 20/20 = 0.00

    Hospital:
        0.5 uniform — consumers, not producers.
        Dependency not meaningful for demand-side nodes.

Graph projection (full supply chain):
    Manufacturing: Factory -[PRODUCES_API]-> API -[COMPONENT_OF]-> Drug
    Physical:      Factory -[PRODUCES_DRUG]-> Drug -[STOCKS]-> Distributor
                                                    -[DELIVERS_TO]-> Hospital
    Demand:        Hospital -[NEEDS_DRUG]-> Drug
    All UNDIRECTED for betweenness.
"""

import warnings
warnings.filterwarnings("ignore")

from neo4j import GraphDatabase
from collections import defaultdict

NEO4J_URI      = "neo4j://127.0.0.1:7687"
NEO4J_USER     = "neo4j"
NEO4J_PASSWORD = "QWEasd123"

GRAPH_NAME         = "supply_chain_full"
CENTRALITY_WEIGHT  = 0.2
DEPENDENCY_WEIGHT  = 0.8

CRITICALITY_WEIGHTS = {
    "Life-Critical": 1.00,
    "High":          0.75,
    "Moderate":      0.50,
    "Low":           0.25,
}

driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

def run(cypher, params=None):
    with driver.session() as s:
        return [dict(r) for r in s.run(cypher, params or {})]

def run_write(cypher, params=None):
    with driver.session() as s:
        s.run(cypher, params or {})


# ══════════════════════════════════════════════════════════════════════════════
# PART 1 — BETWEENNESS CENTRALITY → centralityScore
# ══════════════════════════════════════════════════════════════════════════════

def drop_projection_if_exists():
    exists = run("""
        CALL gds.graph.exists($name) YIELD exists RETURN exists
    """, {"name": GRAPH_NAME})[0]["exists"]
    if exists:
        with driver.session() as s:
            s.run("CALL gds.graph.drop($name)", {"name": GRAPH_NAME})
        print("  Dropped existing projection.")
    else:
        print("  No existing projection found.")


def project_graph():
    print("  Projecting full supply chain graph...")
    result = run("""
        CALL gds.graph.project(
            $name,
            ['Factory', 'API', 'Drug', 'Distributor', 'Hospital'],
            {
                PRODUCES_API:  { orientation: 'UNDIRECTED' },
                COMPONENT_OF:  { orientation: 'UNDIRECTED' },
                PRODUCES_DRUG: { orientation: 'UNDIRECTED' },
                STOCKS:        { orientation: 'UNDIRECTED' },
                DELIVERS_TO:   { orientation: 'UNDIRECTED' },
                NEEDS_DRUG:    { orientation: 'UNDIRECTED' }
            }
        )
        YIELD nodeCount, relationshipCount
        RETURN nodeCount, relationshipCount
    """, {"name": GRAPH_NAME})
    r = result[0]
    print(f"  {r['nodeCount']} nodes, {r['relationshipCount']} relationships")


def run_betweenness():
    """Write betweenness centrality, normalise per label → centralityScore."""
    print("  Running betweenness centrality...")

    # Write raw scores
    run("""
        CALL gds.betweenness.write($name, { writeProperty: 'betweennessRaw' })
        YIELD nodePropertiesWritten RETURN nodePropertiesWritten
    """, {"name": GRAPH_NAME})

    # Per-label min-max normalisation → centralityScore
    labels = ["Factory", "API", "Drug", "Distributor", "Hospital"]
    for label in labels:
        stats = run(f"""
            MATCH (n:{label}) WHERE n.betweennessRaw IS NOT NULL
            RETURN min(n.betweennessRaw) AS mn, max(n.betweennessRaw) AS mx
        """)
        if not stats or stats[0]["mx"] is None:
            continue
        mn, mx = stats[0]["mn"], stats[0]["mx"]

        if mx == mn:
            # All nodes structurally equivalent → uniform 0.5
            run_write(f"""
                MATCH (n:{label}) WHERE n.betweennessRaw IS NOT NULL
                SET n.centralityScore = 0.5
            """)
        else:
            run_write(f"""
                MATCH (n:{label}) WHERE n.betweennessRaw IS NOT NULL
                SET n.centralityScore = round(
                    (n.betweennessRaw - $mn) / ($mx - $mn), 4)
            """, {"mn": mn, "mx": mx})

    run_write("MATCH (n) WHERE n.betweennessRaw IS NOT NULL REMOVE n.betweennessRaw")
    print("  centralityScore written to all nodes.")


# ══════════════════════════════════════════════════════════════════════════════
# PART 2 — DEPENDENCY SCORE → dependencyScore
# ══════════════════════════════════════════════════════════════════════════════

def compute_dependency_scores():
    print("  Computing dependency scores...")

    # ── Fetch base data from Neo4j ────────────────────────────────────────────
    # All factory-API capacity shares
    factory_api_rows = run("""
        MATCH (f:Factory)-[r:PRODUCES_API]->(a:API)
        RETURN f.id AS fid, a.id AS aid, r.capacityShare AS share
    """)

    # All API-Drug relationships with drug criticality
    api_drug_rows = run("""
        MATCH (a:API)-[:COMPONENT_OF]->(d:Drug)
        RETURN a.id AS aid, d.id AS did, d.criticality AS criticality
    """)

    # All Drug-Distributor STOCKS relationships
    dist_drug_rows = run("""
        MATCH (d:Drug)-[:STOCKS]->(s:Distributor)
        RETURN s.id AS sid, count(d) AS drugs_carried
    """)

    total_drugs = run("MATCH (d:Drug) RETURN count(d) AS c")[0]["c"]
    total_apis  = len(set(r["aid"] for r in factory_api_rows))

    # ── Build lookup structures ───────────────────────────────────────────────
    # Per API: number of producers, min capacity share
    api_producers = defaultdict(list)
    for r in factory_api_rows:
        api_producers[r["aid"]].append(float(r["share"]))

    # Per API: max criticality weight of downstream drugs
    api_crit_weight = defaultdict(float)
    for r in api_drug_rows:
        w = CRITICALITY_WEIGHTS.get(r["criticality"], 0.5)
        api_crit_weight[r["aid"]] = max(api_crit_weight[r["aid"]], w)

    # Per factory: num APIs produced, max capacity share
    factory_apis = defaultdict(list)
    factory_max_share = defaultdict(float)
    for r in factory_api_rows:
        factory_apis[r["fid"]].append(r["aid"])
        factory_max_share[r["fid"]] = max(
            factory_max_share[r["fid"]], float(r["share"]))

    # ── Compute and write API dependency ──────────────────────────────────────
    # base_dep: 1.0 if sole producer, else 1 - min_share (safety margin)
    # weighted by max downstream drug criticality
    api_dep_map = {}
    for aid, shares in api_producers.items():
        if len(shares) == 1:
            base_dep = 1.0
        else:
            base_dep = round(1.0 - min(shares), 4)
        crit_w = api_crit_weight.get(aid, 0.5)
        dep = round(base_dep * crit_w, 4)
        api_dep_map[aid] = dep
        run_write("""
            MATCH (a:API {id: $id}) SET a.dependencyScore = $score
        """, {"id": aid, "score": dep})

    # ── Compute and write Factory dependency ──────────────────────────────────
    # max(max_capacity_share, num_apis/total_apis)
    # Captures both depth (how dominant on one API) and
    # breadth (how many APIs simultaneously affected if lost)
    for fid, apis in factory_apis.items():
        max_share = factory_max_share[fid]  # already a decimal [0,1]
        breadth   = len(apis) / total_apis
        dep       = round(max(max_share, breadth), 4)
        run_write("""
            MATCH (f:Factory {id: $id}) SET f.dependencyScore = $score
        """, {"id": fid, "score": dep})

    # ── Compute and write Drug dependency ─────────────────────────────────────
    # Inherits max dependency score from its source APIs.
    # A drug is as vulnerable as its most vulnerable API.
    drug_dep = defaultdict(float)
    for r in api_drug_rows:
        aid = r["aid"]
        did = r["did"]
        drug_dep[did] = max(drug_dep[did], api_dep_map.get(aid, 0.0))

    for did, dep in drug_dep.items():
        run_write("""
            MATCH (d:Drug {id: $id}) SET d.dependencyScore = $score
        """, {"id": did, "score": round(dep, 4)})

    # ── Compute and write Distributor dependency ───────────────────────────────
    # 1 - (drugs_carried / total_drugs)
    # Specialist (few drugs) = high dep. General (all drugs) = low dep.
    for r in dist_drug_rows:
        dep = round(1.0 - (r["drugs_carried"] / total_drugs), 4)
        run_write("""
            MATCH (s:Distributor {id: $id}) SET s.dependencyScore = $score
        """, {"id": r["sid"], "score": dep})

    # ── Hospital: uniform 0.5 ─────────────────────────────────────────────────
    run_write("MATCH (h:Hospital) SET h.dependencyScore = 0.5")

    print("  dependencyScore written to all nodes.")


# ══════════════════════════════════════════════════════════════════════════════
# PART 3 — COMBINED vulnerabilityScore
# ══════════════════════════════════════════════════════════════════════════════

def compute_vulnerability_score():
    print("  Computing combined vulnerabilityScore...")
    run_write(f"""
        MATCH (n)
        WHERE n.centralityScore IS NOT NULL
          AND n.dependencyScore IS NOT NULL
        SET n.vulnerabilityScore = round(
            {CENTRALITY_WEIGHT} * n.centralityScore +
            {DEPENDENCY_WEIGHT} * n.dependencyScore,
        4)
    """)
    print(f"  vulnerabilityScore = "
          f"{CENTRALITY_WEIGHT}×centrality + "
          f"{DEPENDENCY_WEIGHT}×dependency")


# ══════════════════════════════════════════════════════════════════════════════
# PART 4 — VULNERABILITY REPORT
# ══════════════════════════════════════════════════════════════════════════════

def print_report():
    print("\n" + "="*72)
    print("  VULNERABILITY REPORT")
    print(f"  Score = {CENTRALITY_WEIGHT}×centrality + {DEPENDENCY_WEIGHT}×dependency")
    print("="*72)

    labels = ["Factory", "API", "Drug", "Distributor", "Hospital"]
    for label in labels:
        results = run(f"""
            MATCH (n:{label})
            WHERE n.vulnerabilityScore IS NOT NULL
            RETURN n.id AS id, n.name AS name,
                   n.centralityScore   AS cs,
                   n.dependencyScore   AS ds,
                   n.vulnerabilityScore AS vs
            ORDER BY vs DESC
        """)
        print(f"\n  {label}:")
        print(f"    {'ID':<6} {'Name':<32} {'Centrality':>10} "
              f"{'Dependency':>10} {'Vulnerability':>13}")
        print(f"    {'─'*6} {'─'*32} {'─'*10} {'─'*10} {'─'*13}")
        for r in results:
            bar = "█" * int(r["vs"] * 15)
            print(f"    {r['id']:<6} {str(r['name']):<32} "
                  f"{r['cs']:>10.4f} {r['ds']:>10.4f} "
                  f"{r['vs']:>10.4f}  {bar}")

    print("\n" + "="*72)


# ══════════════════════════════════════════════════════════════════════════════
# PART 5 — SPOT CHECKS
# ══════════════════════════════════════════════════════════════════════════════

def spot_checks():
    print("\n  Key assertions:")

    checks = [
        # (description, id_a, label_a, id_b, label_b, expectation)
        ("A012 vs A004 — sole Life-Critical API vs dual-source API",
         "A012", "API", "A004", "API", "A012 > A004"),
        ("A001 vs A007 — both sole-source, Insulin(LC) vs Paracetamol(Mod)",
         "A001", "API", "A007", "API", "A001 > A007"),
        ("F002 vs F005 — dominant+sole vs partial-only factory",
         "F002", "Factory", "F005", "Factory", "F002 > F005"),
        ("D017 vs D004 — Life-Critical sole-source vs Moderate dual-source",
         "D017", "Drug", "D004", "Drug", "D017 > D004"),
        ("D001 vs D008 — Insulin(LC,sole) vs Calpol(Mod,sole)",
         "D001", "Drug", "D008", "Drug", "D001 > D008"),
        ("S007 vs S001 — specialist(1 drug) vs general(20 drugs)",
         "S007", "Distributor", "S001", "Distributor", "S007 > S001"),
    ]

    all_pass = True
    for desc, id_a, label_a, id_b, label_b, expectation in checks:
        sa = run(f"MATCH (n:{label_a} {{id:$id}}) RETURN n.vulnerabilityScore AS s",
                 {"id": id_a})[0]["s"]
        sb = run(f"MATCH (n:{label_b} {{id:$id}}) RETURN n.vulnerabilityScore AS s",
                 {"id": id_b})[0]["s"]
        passed = sa > sb
        verdict = "✓" if passed else "✗"
        if not passed:
            all_pass = False
        ids = expectation.split(" > ")
        print(f"  {verdict} {desc}")
        print(f"    {ids[0]}={sa:.4f}  {ids[1]}={sb:.4f}  "
              f"{'PASS' if passed else 'FAIL'}")

    print()
    if all_pass:
        print("  All assertions passed ✓")
    else:
        print("  Some assertions failed — review scores above.")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("\n" + "="*72)
    print("  GNN COMPONENT 1 — STRUCTURAL VULNERABILITY ANALYSIS")
    print("  Betweenness Centrality + Criticality-Weighted Dependency Score")
    print("="*72 + "\n")

    print("── Part 1: Betweenness Centrality ───────────────────────────────────")
    drop_projection_if_exists()
    project_graph()
    run_betweenness()
    with driver.session() as s:
        s.run("CALL gds.graph.drop($name)", {"name": GRAPH_NAME})
    print("  GDS projection dropped from memory.")

    print("\n── Part 2: Dependency Score ─────────────────────────────────────────")
    compute_dependency_scores()

    print("\n── Part 3: Combined Vulnerability Score ─────────────────────────────")
    compute_vulnerability_score()

    print("\n── Part 4: Vulnerability Report ─────────────────────────────────────")
    print_report()

    print("\n── Part 5: Spot Checks ──────────────────────────────────────────────")
    spot_checks()

    print("  centralityScore, dependencyScore, vulnerabilityScore")
    print("  written permanently to all Neo4j nodes.")
    print("  Prediction engine score remains a completely separate signal.\n")

    driver.close()