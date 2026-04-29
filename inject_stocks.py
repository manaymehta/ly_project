"""
inject_stocks.py

Adds STOCKS relationships to Neo4j: Drug -[:STOCKS]-> Distributor

Derived from distributor_catalogue.csv — if a distributor carries a drug
with stock > 0, a STOCKS edge is created between that Drug node and that
Distributor node.

This makes the full supply chain path traversable in one connected graph:
    Factory -[:PRODUCES_DRUG]-> Drug -[:STOCKS]-> Distributor -[:DELIVERS_TO]-> Hospital

Required for GNN betweenness centrality to compute meaningful scores
across the full manufacturing + delivery chain.

Safe to run multiple times — uses MERGE, never duplicates.
"""

import pandas as pd
from neo4j import GraphDatabase

NEO4J_URI      = "neo4j://127.0.0.1:7687"
NEO4J_USER     = "neo4j"
NEO4J_PASSWORD = "QWEasd123"
DATA_DIR       = "."

driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

def run(cypher, params=None):
    with driver.session() as s:
        return [dict(r) for r in s.run(cypher, params or {})]

def run_write(cypher, params=None):
    with driver.session() as s:
        s.run(cypher, params or {})

# ── Load catalogue ────────────────────────────────────────────────────────────
print("Loading distributor_catalogue.csv...")
catalogue = pd.read_csv(f"{DATA_DIR}/distributor_catalogue.csv",
                        encoding="cp1252", usecols=range(7))
catalogue.columns = catalogue.columns.str.strip()

# Only drugs with stock > 0 — no point connecting a distributor
# that has no stock of a given drug
catalogue_valid = catalogue[catalogue["Current Stock"] > 0].dropna(
    subset=["Drug ID", "Distributor ID"])

# Unique drug-distributor pairs only — one STOCKS edge per pair
# regardless of how many hospitals that distributor serves for that drug
unique_pairs = catalogue_valid[["Drug ID", "Distributor ID"]].drop_duplicates()
print(f"Unique Drug→Distributor pairs with stock > 0: {len(unique_pairs)}")

# ── Check existing STOCKS relationships ───────────────────────────────────────
existing = run("MATCH ()-[r:STOCKS]->() RETURN count(r) AS c")[0]["c"]
print(f"Existing STOCKS relationships in Neo4j: {existing}")

# ── Inject STOCKS relationships ───────────────────────────────────────────────
print("Injecting STOCKS relationships (MERGE — safe to rerun)...")
created = 0
for _, row in unique_pairs.iterrows():
    run_write("""
        MATCH (d:Drug {id: $drug_id})
        MATCH (s:Distributor {id: $dist_id})
        MERGE (d)-[:STOCKS]->(s)
    """, {
        "drug_id": str(row["Drug ID"]).strip(),
        "dist_id": str(row["Distributor ID"]).strip(),
    })
    created += 1

print(f"Processed {created} pairs.")

# ── Verify ────────────────────────────────────────────────────────────────────
total = run("MATCH ()-[r:STOCKS]->() RETURN count(r) AS c")[0]["c"]
print(f"Total STOCKS relationships now in Neo4j: {total}")

# Spot check — D017 Salbutamol should connect to its distributors
d017_dists = run("""
    MATCH (d:Drug {id:'D017'})-[:STOCKS]->(s:Distributor)
    RETURN s.id AS dist_id, s.name AS name
    ORDER BY dist_id
""")
print(f"\nD017 Salbutamol stocks distributors ({len(d017_dists)}):")
for r in d017_dists:
    print(f"  {r['dist_id']} {r['name']}")

# Verify full path is now traversable
print("\nFull path check (Factory→Drug→Distributor→Hospital):")
path_check = run("""
    MATCH (f:Factory {id:'F002'})-[:PRODUCES_DRUG]->(d:Drug)
          -[:STOCKS]->(s:Distributor)-[:DELIVERS_TO]->(h:Hospital)
    RETURN f.name AS factory, d.name AS drug,
           s.name AS distributor, h.name AS hospital
    LIMIT 3
""")
if path_check:
    for r in path_check:
        print(f"  {r['factory']} → {r['drug']} → {r['distributor']} → {r['hospital']}")
    print("  ✓ Full path traversable")
else:
    print("  ✗ Path not found — check relationships")

driver.close()
print("\nDone. STOCKS relationships injected.")