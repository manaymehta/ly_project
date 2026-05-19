"""
seed_neo4j.py

Full Neo4j graph seeding script for the LY Project.
Run this ONCE on a fresh Neo4j database to create all nodes and relationships.

Order of operations:
    1. Clear existing data
    2. Create nodes: Factory, API, Drug, Distributor, Hospital
    3. Create relationships: PRODUCES_API, COMPONENT_OF, PRODUCES_DRUG (derived),
                            DELIVERS_TO, NEEDS_DRUG, ALTERNATIVE_TO, STOCKS

Run:
    conda activate ml_env
    python scripts/seed_neo4j.py

Safe to re-run — clears and rebuilds from scratch each time.
"""

import os
from pathlib import Path
import pandas as pd
from neo4j import GraphDatabase
from dotenv import load_dotenv

load_dotenv()

DATA_DIR = Path(__file__).parent.parent / "data"

NEO4J_URI      = os.environ["NEO4J_URI"]
NEO4J_USER     = os.environ["NEO4J_USER"]
NEO4J_PASSWORD = os.environ["NEO4J_PASSWORD"]

driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

def run(cypher, params=None):
    with driver.session() as s:
        return [dict(r) for r in s.run(cypher, params or {})]

def run_write(cypher, params=None):
    with driver.session() as s:
        s.run(cypher, params or {})

def count(label):
    return run(f"MATCH (n:{label}) RETURN count(n) AS c")[0]["c"]

def count_rel(rel):
    return run(f"MATCH ()-[r:{rel}]->() RETURN count(r) AS c")[0]["c"]

# ── 0. Clear ──────────────────────────────────────────────────────────────────

print("\n[1/8] Clearing existing graph...")
run_write("MATCH (n) DETACH DELETE n")
print("      Cleared.")

# ── 1. Hospitals ──────────────────────────────────────────────────────────────

print("[2/8] Creating Hospital nodes...")
hospitals = pd.read_csv(DATA_DIR / "hospitals.csv")
for _, row in hospitals.iterrows():
    run_write("""
        CREATE (:Hospital {
            id:            $id,
            name:          $name,
            city:          $city,
            specialty:     $specialty,
            specialtyType: $specialty_type,
            avgDailyPatients: $patients
        })
    """, {
        "id":            str(row["Hospital ID"]).strip(),
        "name":          str(row["Name"]).strip(),
        "city":          str(row["City"]).strip(),
        "specialty":     str(row["Specialty Focus"]).strip(),
        "specialty_type": str(row["Specialty Type"]).strip(),
        "patients":      str(row["Avg Daily Patients"]).replace(",", "").strip(),
    })
print(f"      {count('Hospital')} Hospital nodes created.")

# ── 2. Factories ──────────────────────────────────────────────────────────────

print("[3/8] Creating Factory nodes...")
factories = pd.read_csv(DATA_DIR / "factories.csv")
for _, row in factories.iterrows():
    run_write("""
        CREATE (:Factory {
            id:              $id,
            name:            $name,
            city:            $city,
            specialization:  $spec,
            monthlyCapacity: $capacity,
            reliabilityScore: $reliability
        })
    """, {
        "id":          str(row["Factory ID"]).strip(),
        "name":        str(row["Name"]).strip(),
        "city":        str(row["City"]).strip(),
        "spec":        str(row["Specialization"]).strip(),
        "capacity":    str(row["Monthly Capacity (units)"]).replace(",", "").strip(),
        "reliability": float(row["Reliability Score"]),
    })
print(f"      {count('Factory')} Factory nodes created.")

# ── 3. APIs ───────────────────────────────────────────────────────────────────

print("[4/8] Creating API nodes...")
apis = pd.read_csv(DATA_DIR / "apis.csv")
for _, row in apis.iterrows():
    run_write("""
        CREATE (:API {
            id:              $id,
            name:            $name,
            category:        $category,
            complexityFactor: $complexity
        })
    """, {
        "id":         str(row["API ID"]).strip(),
        "name":       str(row["Name"]).strip(),
        "category":   str(row["Category"]).strip(),
        "complexity": float(row["Complexity Factor"]),
    })
print(f"      {count('API')} API nodes created.")

# ── 4. Drugs ──────────────────────────────────────────────────────────────────

print("[5/8] Creating Drug nodes...")
drugs = pd.read_csv(DATA_DIR / "drugs.csv")
for _, row in drugs.iterrows():
    run_write("""
        CREATE (:Drug {
            id:               $id,
            name:             $name,
            genericName:      $generic,
            apiUsed:          $api,
            category:         $category,
            criticality:      $criticality,
            seasonalityProfile: $seasonality,
            consumptionType:  $consumption,
            basePrice:        $price
        })
    """, {
        "id":          str(row["Drug ID"]).strip(),
        "name":        str(row["Brand Name"]).strip(),
        "generic":     str(row["Generic Name"]).strip(),
        "api":         str(row["API Used"]).strip(),
        "category":    str(row["Category"]).strip(),
        "criticality": str(row["Criticality"]).strip(),
        "seasonality": str(row["Seasonality Profile"]).strip(),
        "consumption": str(row["Consumption Type"]).strip(),
        "price":       float(str(row["Base Price (?/unit)"]).replace(",", "")),
    })
print(f"      {count('Drug')} Drug nodes created.")

# ── 5. Distributors ───────────────────────────────────────────────────────────

print("[6/8] Creating Distributor nodes...")
distributors = pd.read_csv(DATA_DIR / "distributors.csv")
for _, row in distributors.iterrows():
    run_write("""
        CREATE (:Distributor {
            id:              $id,
            name:            $name,
            city:            $city,
            type:            $type,
            reliabilityScore: $reliability,
            pricingTier:     $pricing,
            deliverySpeed:   $speed
        })
    """, {
        "id":          str(row["Distributor ID"]).strip(),
        "name":        str(row["Name"]).strip(),
        "city":        str(row["City"]).strip(),
        "type":        str(row["Type"]).strip(),
        "reliability": float(row["Reliability Score"]),
        "pricing":     str(row["Pricing Tier"]).strip(),
        "speed":       str(row["Delivery Speed Class"]).strip(),
    })
print(f"      {count('Distributor')} Distributor nodes created.")

# ── 6. Relationships ──────────────────────────────────────────────────────────

print("[7/8] Creating relationships...")

# PRODUCES_API — Factory uses API to produce drugs (capacityShare = fraction of global supply)
factory_api = pd.read_csv(DATA_DIR / "factory_api_map.csv")
for _, row in factory_api.iterrows():
    run_write("""
        MATCH (f:Factory {id: $fid}), (a:API {id: $aid})
        CREATE (f)-[:PRODUCES_API {
            monthlyOutput: $output,
            capacityShare: $share
        }]->(a)
    """, {
        "fid":    str(row["Factory ID"]).strip(),
        "aid":    str(row["API ID"]).strip(),
        "output": int(str(row["Monthly Output (units)"]).replace(",", "")),
        "share":  float(row["Capacity Share (%)"]) / 100,
    })
print(f"      {count_rel('PRODUCES_API')} PRODUCES_API relationships.")

# COMPONENT_OF — API is a component of Drug
api_drug = pd.read_csv(DATA_DIR / "api_drug_map.csv")
for _, row in api_drug.iterrows():
    run_write("""
        MATCH (a:API {id: $aid}), (d:Drug {id: $did})
        CREATE (a)-[:COMPONENT_OF {yieldMultiplier: $yield}]->(d)
    """, {
        "aid":   str(row["API ID"]).strip(),
        "did":   str(row["Drug ID"]).strip(),
        "yield": float(row["Yield Multiplier"]),
    })
print(f"      {count_rel('COMPONENT_OF')} COMPONENT_OF relationships.")

# PRODUCES_DRUG — derived shortcut: Factory → Drug (via Factory→API→Drug)
run_write("""
    MATCH (f:Factory)-[:PRODUCES_API]->(a:API)-[:COMPONENT_OF]->(d:Drug)
    MERGE (f)-[:PRODUCES_DRUG]->(d)
""")
print(f"      {count_rel('PRODUCES_DRUG')} PRODUCES_DRUG relationships (derived).")

# NEEDS_DRUG — Hospital → Drug (daily demand, current inventory)
hospital_demand = pd.read_csv(DATA_DIR / "hospital_drug_demand.csv")
for _, row in hospital_demand.iterrows():
    run_write("""
        MATCH (h:Hospital {id: $hid}), (d:Drug {id: $did})
        CREATE (h)-[:NEEDS_DRUG {
            dailyDemand:   $daily,
            monthlyDemand: $monthly,
            currentUnits:  $units,
            daysOfStock:   $days
        }]->(d)
    """, {
        "hid":     str(row["Hospital ID"]).strip(),
        "did":     str(row["Drug ID"]).strip(),
        "daily":   float(row["Daily Demand"]),
        "monthly": float(row["Monthly Demand"]),
        "units":   float(row["Current Units"]),
        "days":    float(row["Days of Stock"]),
    })
print(f"      {count_rel('NEEDS_DRUG')} NEEDS_DRUG relationships.")

# DELIVERS_TO — Distributor → Hospital (per drug, with stock/pricing/delivery)
catalogue = pd.read_csv(DATA_DIR / "distributor_catalogue.csv", encoding="cp1252", usecols=range(7))
# Rename columns positionally to avoid encoding issues with special characters in header
catalogue.columns = ["distributor_id", "drug_id", "hospital_id", "price_per_unit", "min_order", "delivery_days", "current_stock"]
catalogue = catalogue.dropna(subset=["distributor_id", "drug_id", "hospital_id"])
for _, row in catalogue.iterrows():
    run_write("""
        MATCH (s:Distributor {id: $sid}), (h:Hospital {id: $hid})
        CREATE (s)-[:DELIVERS_TO {
            drugId:       $drug_id,
            pricePerUnit: $price,
            minOrder:     $min_order,
            deliveryDays: $delivery_days,
            currentStock: $stock
        }]->(h)
    """, {
        "sid":           str(row["distributor_id"]).strip(),
        "hid":           str(row["hospital_id"]).strip(),
        "drug_id":       str(row["drug_id"]).strip(),
        "price":         float(row["price_per_unit"]),
        "min_order":     float(row["min_order"]),
        "delivery_days": float(row["delivery_days"]),
        "stock":         float(row["current_stock"]),
    })
print(f"      {count_rel('DELIVERS_TO')} DELIVERS_TO relationships.")

# STOCKS — Drug → Distributor (derived from catalogue where stock > 0)
run_write("""
    MATCH (s:Distributor)-[r:DELIVERS_TO]->()
    WITH DISTINCT s, r.drugId AS drugId
    MATCH (d:Drug {id: drugId})
    MERGE (d)-[:STOCKS]->(s)
""")
print(f"      {count_rel('STOCKS')} STOCKS relationships (derived).")

# ALTERNATIVE_TO — Drug → Drug substitution options
alt_map = pd.read_csv(DATA_DIR / "alt_drug_map.csv")
alt_map = alt_map.dropna(subset=["Alternative Drug ID"])
for _, row in alt_map.iterrows():
    try:
        score = float(row["Similarity Score"]) if pd.notna(row["Similarity Score"]) else 0.0
        shared = str(row["Shared API Risk"]).strip().lower() == "yes"
        run_write("""
            MATCH (d1:Drug {id: $did}), (d2:Drug {id: $alt_id})
            CREATE (d1)-[:ALTERNATIVE_TO {
                similarityScore: $score,
                sharedApiRisk:   $shared,
                notes:           $notes
            }]->(d2)
        """, {
            "did":    str(row["Drug ID"]).strip(),
            "alt_id": str(row["Alternative Drug ID"]).strip(),
            "score":  score,
            "shared": shared,
            "notes":  str(row["Substitution Notes"]).strip() if pd.notna(row.get("Substitution Notes")) else "",
        })
    except Exception:
        continue
print(f"      {count_rel('ALTERNATIVE_TO')} ALTERNATIVE_TO relationships.")

# ── 7. Indexes ────────────────────────────────────────────────────────────────

print("[8/8] Creating indexes...")
for label, prop in [("Hospital","id"), ("Factory","id"), ("API","id"),
                    ("Drug","id"), ("Distributor","id")]:
    try:
        run_write(f"CREATE INDEX IF NOT EXISTS FOR (n:{label}) ON (n.{prop})")
    except Exception:
        pass
print("      Indexes created.")

# ── Summary ───────────────────────────────────────────────────────────────────

print("\n" + "="*50)
print("  NEO4J SEEDING COMPLETE")
print("="*50)
print(f"  Hospitals    : {count('Hospital')}")
print(f"  Factories    : {count('Factory')}")
print(f"  APIs         : {count('API')}")
print(f"  Drugs        : {count('Drug')}")
print(f"  Distributors : {count('Distributor')}")
print(f"  PRODUCES_API : {count_rel('PRODUCES_API')}")
print(f"  COMPONENT_OF : {count_rel('COMPONENT_OF')}")
print(f"  PRODUCES_DRUG: {count_rel('PRODUCES_DRUG')}")
print(f"  NEEDS_DRUG   : {count_rel('NEEDS_DRUG')}")
print(f"  DELIVERS_TO  : {count_rel('DELIVERS_TO')}")
print(f"  STOCKS       : {count_rel('STOCKS')}")
print(f"  ALTERNATIVE_TO:{count_rel('ALTERNATIVE_TO')}")
print("="*50)
print("\nNext step: run  python ml/gnn_centrality.py  to compute vulnerability scores.")

driver.close()
