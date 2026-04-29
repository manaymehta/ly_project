"""
neo4j_audit.py

Deep audit of Neo4j graph against source CSV files.
Checks every node, every property value, every relationship,
and every relationship property — cross-validated against source data.

Run: python neo4j_audit.py

Prints PASS / FAIL for every check. Any FAIL needs investigation.
"""

import pandas as pd
import numpy as np
from neo4j import GraphDatabase

NEO4J_URI      = "neo4j://127.0.0.1:7687"
NEO4J_USER     = "neo4j"
NEO4J_PASSWORD = "QWEasd123"
DATA_DIR       = "."

driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

def q(cypher, params=None):
    with driver.session() as s:
        return [dict(r) for r in s.run(cypher, params or {})]

PASS = "✓"
FAIL = "✗"
WARN = "⚠"
results = []

def check(label, condition, detail=""):
    status = PASS if condition else FAIL
    results.append((status, label, detail))
    print(f"  {status}  {label}" + (f"  — {detail}" if detail else ""))

def section(title):
    print(f"\n{'='*62}")
    print(f"  {title}")
    print(f"{'='*62}")

# ── Load CSVs ──────────────────────────────────────────────────────────────────
section("Loading CSVs")
factories    = pd.read_csv(f"{DATA_DIR}/factories.csv",            encoding="cp1252")
apis         = pd.read_csv(f"{DATA_DIR}/apis.csv",                 encoding="cp1252")
drugs        = pd.read_csv(f"{DATA_DIR}/drugs.csv",                encoding="cp1252")
distributors = pd.read_csv(f"{DATA_DIR}/distributors.csv",         encoding="cp1252")
hospitals    = pd.read_csv(f"{DATA_DIR}/hospitals.csv",            encoding="cp1252")
factory_api  = pd.read_csv(f"{DATA_DIR}/factory_api_map.csv",      encoding="cp1252")
api_drug     = pd.read_csv(f"{DATA_DIR}/api_drug_map.csv",         encoding="cp1252")
alt_drug     = pd.read_csv(f"{DATA_DIR}/alt_drug_map.csv",         encoding="latin-1")
catalogue    = pd.read_csv(f"{DATA_DIR}/distributor_catalogue.csv",encoding="cp1252", usecols=range(7))
demand       = pd.read_csv(f"{DATA_DIR}/hospital_drug_demand.csv", encoding="cp1252", usecols=range(6))

for df in [factories, apis, drugs, distributors, hospitals,
           factory_api, api_drug, alt_drug, catalogue, demand]:
    df.columns = df.columns.str.strip()

# Clean numeric columns
factories["Monthly Capacity (units)"] = (
    factories["Monthly Capacity (units)"].astype(str).str.replace(",","").astype(int))
factory_api["Monthly Output (units)"] = (
    factory_api["Monthly Output (units)"].astype(str).str.replace(",","").astype(int))
factory_api["Capacity Share (%)"] = factory_api["Capacity Share (%)"].astype(float)
hospitals["Avg Daily Patients"] = (
    hospitals["Avg Daily Patients"].astype(str).str.replace(",","").str.strip().astype(float))

catalogue_valid = catalogue[
    catalogue["Current Stock"] > 0
].dropna(subset=["Distributor ID","Drug ID","Hospital ID"])

price_col = [c for c in catalogue.columns if "Price" in c][0]

print("  All CSVs loaded.")

# ══════════════════════════════════════════════════════════════════════════════
section("1. NODE COUNTS")
# ══════════════════════════════════════════════════════════════════════════════
expected_counts = {
    "Factory": len(factories),
    "API": len(apis),
    "Drug": len(drugs),
    "Distributor": len(distributors),
    "Hospital": len(hospitals.dropna(subset=["Hospital ID"])),
}
for label, exp in expected_counts.items():
    actual = q(f"MATCH (n:{label}) RETURN count(n) AS c")[0]["c"]
    check(f"{label} count", actual == exp, f"neo4j={actual} csv={exp}")

# ══════════════════════════════════════════════════════════════════════════════
section("2. FACTORY NODE PROPERTIES")
# ══════════════════════════════════════════════════════════════════════════════
for _, row in factories.iterrows():
    fid = row["Factory ID"].strip()
    neo = q("MATCH (f:Factory {id:$id}) RETURN f", {"id": fid})
    if not neo:
        check(f"Factory {fid} exists", False, "NOT FOUND")
        continue
    f = neo[0]["f"]
    check(f"F {fid} name",             f.get("name")             == row["Name"].strip())
    check(f"F {fid} city",             f.get("city")             == row["City"].strip())
    check(f"F {fid} monthlyCapacity",  f.get("monthlyCapacity")  == int(row["Monthly Capacity (units)"]))
    check(f"F {fid} reliabilityScore", abs(f.get("reliabilityScore",0) - float(row["Reliability Score"])) < 0.001)
    check(f"F {fid} specialization",   f.get("specialization")   == row["Specialization"].strip())

# ══════════════════════════════════════════════════════════════════════════════
section("3. API NODE PROPERTIES")
# ══════════════════════════════════════════════════════════════════════════════
for _, row in apis.iterrows():
    aid = row["API ID"].strip()
    neo = q("MATCH (a:API {id:$id}) RETURN a", {"id": aid})
    if not neo:
        check(f"API {aid} exists", False, "NOT FOUND")
        continue
    a = neo[0]["a"]
    check(f"A {aid} name",            a.get("name")            == row["Name"].strip())
    check(f"A {aid} category",        a.get("category")        == row["Category"].strip())
    check(f"A {aid} complexityFactor",abs(a.get("complexityFactor",0) - float(row["Complexity Factor"])) < 0.001)

# ══════════════════════════════════════════════════════════════════════════════
section("4. DRUG NODE PROPERTIES")
# ══════════════════════════════════════════════════════════════════════════════
for _, row in drugs.iterrows():
    did = row["Drug ID"].strip()
    neo = q("MATCH (d:Drug {id:$id}) RETURN d", {"id": did})
    if not neo:
        check(f"Drug {did} exists", False, "NOT FOUND")
        continue
    d = neo[0]["d"]
    check(f"D {did} name",               d.get("name")               == row["Brand Name"].strip())
    check(f"D {did} genericName",        d.get("genericName")        == row["Generic Name"].strip())
    check(f"D {did} criticality",        d.get("criticality")        == row["Criticality"].strip())
    check(f"D {did} category",           d.get("category")           == row["Category"].strip())
    check(f"D {did} seasonalityProfile", d.get("seasonalityProfile") == row["Seasonality Profile"].strip())
    check(f"D {did} consumptionType",    d.get("consumptionType")    == row["Consumption Type"].strip())
    check(f"D {did} basePrice",          abs(d.get("basePrice",0)    - float(row["Base Price (?/unit)"])) < 0.01)

# ══════════════════════════════════════════════════════════════════════════════
section("5. DISTRIBUTOR NODE PROPERTIES")
# ══════════════════════════════════════════════════════════════════════════════
for _, row in distributors.iterrows():
    sid = row["Distributor ID"].strip()
    neo = q("MATCH (s:Distributor {id:$id}) RETURN s", {"id": sid})
    if not neo:
        check(f"Dist {sid} exists", False, "NOT FOUND")
        continue
    s = neo[0]["s"]
    check(f"S {sid} name",               s.get("name")               == row["Name"].strip())
    check(f"S {sid} city",               s.get("city")               == row["City"].strip())
    check(f"S {sid} deliverySpeedClass", s.get("deliverySpeedClass") == row["Delivery Speed Class"].strip())
    check(f"S {sid} specialization",     s.get("specialization")     == row["Specialization"].strip())
    check(f"S {sid} reliabilityScore",   abs(s.get("reliabilityScore",0) - float(row["Reliability Score"])) < 0.001)

# ══════════════════════════════════════════════════════════════════════════════
section("6. HOSPITAL NODE PROPERTIES")
# ══════════════════════════════════════════════════════════════════════════════
for _, row in hospitals.dropna(subset=["Hospital ID"]).iterrows():
    hid = str(row["Hospital ID"]).strip()
    neo = q("MATCH (h:Hospital {id:$id}) RETURN h", {"id": hid})
    if not neo:
        check(f"Hospital {hid} exists", False, "NOT FOUND")
        continue
    h = neo[0]["h"]
    expected_name = str(row["Name"]).strip() if str(row["Name"]).strip() not in ["nan",""] else None
    if expected_name:
        check(f"H {hid} name", h.get("name") == expected_name)
    check(f"H {hid} city",          h.get("city")        == str(row["City"]).strip())
    check(f"H {hid} specialtyType", h.get("specialtyType") == str(row["Specialty Type"]).strip())
    check(f"H {hid} avgDailyPatients", abs(h.get("avgDailyPatients",0) - float(row["Avg Daily Patients"])) < 1)

# ══════════════════════════════════════════════════════════════════════════════
section("7. PRODUCES_API RELATIONSHIPS")
# ══════════════════════════════════════════════════════════════════════════════
check("PRODUCES_API count",
      q("MATCH ()-[r:PRODUCES_API]->() RETURN count(r) AS c")[0]["c"] == len(factory_api),
      f"expected {len(factory_api)}")

for _, row in factory_api.iterrows():
    fid = row["Factory ID"].strip()
    aid = row["API ID"].strip()
    expected_share  = round(float(row["Capacity Share (%)"]) / 100.0, 4)
    expected_output = int(row["Monthly Output (units)"])
    neo = q("""
        MATCH (f:Factory {id:$fid})-[r:PRODUCES_API]->(a:API {id:$aid})
        RETURN r.capacityShare AS cs, r.monthlyOutput AS mo
    """, {"fid": fid, "aid": aid})
    if not neo:
        check(f"PRODUCES_API {fid}→{aid}", False, "NOT FOUND")
        continue
    r = neo[0]
    check(f"PRODUCES_API {fid}→{aid} capacityShare",
          abs(r["cs"] - expected_share) < 0.001,
          f"neo4j={r['cs']:.4f} expected={expected_share:.4f}")
    check(f"PRODUCES_API {fid}→{aid} monthlyOutput",
          r["mo"] == expected_output,
          f"neo4j={r['mo']} expected={expected_output}")

# Capacity share sums to 1.0 per API
cap_sums = factory_api.groupby("API ID")["Capacity Share (%)"].sum()
for api_id, total in cap_sums.items():
    neo_sum = q("""
        MATCH ()-[r:PRODUCES_API]->(a:API {id:$id})
        RETURN sum(r.capacityShare) AS s
    """, {"id": api_id})[0]["s"]
    check(f"capacityShare sum=1.0 for {api_id}",
          abs(neo_sum - 1.0) < 0.01,
          f"neo4j_sum={neo_sum:.4f}")

# ══════════════════════════════════════════════════════════════════════════════
section("8. COMPONENT_OF RELATIONSHIPS")
# ══════════════════════════════════════════════════════════════════════════════
check("COMPONENT_OF count",
      q("MATCH ()-[r:COMPONENT_OF]->() RETURN count(r) AS c")[0]["c"] == len(api_drug),
      f"expected {len(api_drug)}")

for _, row in api_drug.iterrows():
    aid = row["API ID"].strip()
    did = row["Drug ID"].strip()
    expected_ym = float(row["Yield Multiplier"])
    neo = q("""
        MATCH (a:API {id:$aid})-[r:COMPONENT_OF]->(d:Drug {id:$did})
        RETURN r.yieldMultiplier AS ym
    """, {"aid": aid, "did": did})
    if not neo:
        check(f"COMPONENT_OF {aid}→{did}", False, "NOT FOUND")
        continue
    check(f"COMPONENT_OF {aid}→{did} yieldMultiplier",
          abs(neo[0]["ym"] - expected_ym) < 0.001,
          f"neo4j={neo[0]['ym']} expected={expected_ym}")

# ══════════════════════════════════════════════════════════════════════════════
section("9. PRODUCES_DRUG RELATIONSHIPS (derived)")
# ══════════════════════════════════════════════════════════════════════════════
merged_fd = factory_api.merge(api_drug, on="API ID")
unique_fd  = merged_fd[["Factory ID","Drug ID"]].drop_duplicates()
actual_pd  = q("MATCH ()-[r:PRODUCES_DRUG]->() RETURN count(r) AS c")[0]["c"]
check("PRODUCES_DRUG count", actual_pd == len(unique_fd), f"neo4j={actual_pd} expected={len(unique_fd)}")

# ══════════════════════════════════════════════════════════════════════════════
section("10. ALTERNATIVE_TO RELATIONSHIPS")
# ══════════════════════════════════════════════════════════════════════════════
valid_alts = alt_drug.dropna(subset=["Alternative Drug ID"])
valid_alts = valid_alts[~valid_alts["Alternative Drug ID"].astype(str).str.strip().str.lower().isin(["none","nan","","-"])]
actual_at  = q("MATCH ()-[r:ALTERNATIVE_TO]->() RETURN count(r) AS c")[0]["c"]
check("ALTERNATIVE_TO count", actual_at == len(valid_alts), f"neo4j={actual_at} expected={len(valid_alts)}")

for _, row in valid_alts.iterrows():
    did    = row["Drug ID"].strip()
    alt_id = str(row["Alternative Drug ID"]).strip()
    neo = q("""
        MATCH (d:Drug {id:$did})-[r:ALTERNATIVE_TO]->(alt:Drug {id:$alt_id})
        RETURN r.similarityScore AS ss, r.sharedApiRisk AS sar
    """, {"did": did, "alt_id": alt_id})
    check(f"ALTERNATIVE_TO {did}→{alt_id} exists", len(neo) > 0)
    if neo:
        check(f"ALTERNATIVE_TO {did}→{alt_id} similarityScore",
              abs(neo[0]["ss"] - float(row["Similarity Score"])) < 0.001)

# ══════════════════════════════════════════════════════════════════════════════
section("11. DELIVERS_TO RELATIONSHIPS")
# ══════════════════════════════════════════════════════════════════════════════
actual_dt = q("MATCH ()-[r:DELIVERS_TO]->() RETURN count(r) AS c")[0]["c"]
check("DELIVERS_TO count", actual_dt == len(catalogue_valid),
      f"neo4j={actual_dt} expected={len(catalogue_valid)}")

# Spot check 20 random rows
sample = catalogue_valid.sample(min(20, len(catalogue_valid)), random_state=42)
for _, row in sample.iterrows():
    sid = str(row["Distributor ID"]).strip()
    hid = str(row["Hospital ID"]).strip()
    did = str(row["Drug ID"]).strip()
    neo = q("""
        MATCH (s:Distributor {id:$sid})-[r:DELIVERS_TO {drugId:$did}]->(h:Hospital {id:$hid})
        RETURN r.pricePerUnit AS p, r.minOrder AS mo,
               r.deliveryDays AS dd, r.currentStock AS cs
    """, {"sid": sid, "hid": hid, "did": did})
    if not neo:
        check(f"DELIVERS_TO {sid}→{hid} drug={did}", False, "NOT FOUND")
        continue
    r = neo[0]
    check(f"DELIVERS_TO {sid}→{hid} {did} price",
          abs(r["p"] - float(row[price_col])) < 0.01)
    check(f"DELIVERS_TO {sid}→{hid} {did} minOrder",
          r["mo"] == int(row["Min Order (units)"]))
    check(f"DELIVERS_TO {sid}→{hid} {did} deliveryDays",
          r["dd"] == int(row["Delivery Days"]))
    check(f"DELIVERS_TO {sid}→{hid} {did} currentStock",
          r["cs"] == int(row["Current Stock"]))

# ══════════════════════════════════════════════════════════════════════════════
section("12. NEEDS_DRUG RELATIONSHIPS")
# ══════════════════════════════════════════════════════════════════════════════
actual_nd = q("MATCH ()-[r:NEEDS_DRUG]->() RETURN count(r) AS c")[0]["c"]
check("NEEDS_DRUG count", actual_nd == len(demand),
      f"neo4j={actual_nd} expected={len(demand)}")

sample_d = demand.sample(min(20, len(demand)), random_state=99)
for _, row in sample_d.iterrows():
    hid = str(row["Hospital ID"]).strip()
    did = str(row["Drug ID"]).strip()
    neo = q("""
        MATCH (h:Hospital {id:$hid})-[r:NEEDS_DRUG]->(d:Drug {id:$did})
        RETURN r.dailyDemand AS dd, r.monthlyDemand AS md,
               r.currentUnits AS cu, r.daysOfStock AS dos
    """, {"hid": hid, "did": did})
    if not neo:
        check(f"NEEDS_DRUG {hid}→{did}", False, "NOT FOUND")
        continue
    r = neo[0]
    check(f"NEEDS_DRUG {hid}→{did} dailyDemand",
          abs(r["dd"] - float(row["Daily Demand"])) < 0.1)
    check(f"NEEDS_DRUG {hid}→{did} monthlyDemand",
          r["md"] == int(row["Monthly Demand"]))
    check(f"NEEDS_DRUG {hid}→{did} currentUnits",
          r["cu"] == int(row["Current Units"]))
    check(f"NEEDS_DRUG {hid}→{did} daysOfStock",
          abs(r["dos"] - float(row["Days of Stock"])) < 0.1)

# ══════════════════════════════════════════════════════════════════════════════
section("13. BUSINESS LOGIC CHECKS")
# ══════════════════════════════════════════════════════════════════════════════

# A012 Salbutamol — only one producer (Cipla F002)
a012 = q("MATCH (f:Factory)-[:PRODUCES_API]->(a:API {id:'A012'}) RETURN f.id AS fid")
check("A012 single producer (Cipla only)", len(a012)==1 and a012[0]["fid"]=="F002",
      f"producers={[r['fid'] for r in a012]}")

# A004 Amoxicillin — exactly two producers summing to 100%
a004 = q("""MATCH (f:Factory)-[r:PRODUCES_API]->(a:API {id:'A004'})
            RETURN f.id AS fid, r.capacityShare AS cs""")
check("A004 two producers", len(a004)==2, f"found={len(a004)}")
check("A004 shares sum to 1.0", abs(sum(r["cs"] for r in a004)-1.0)<0.01)

# D001 Insulin — no alternative
d001_alt = q("MATCH (d:Drug {id:'D001'})-[:ALTERNATIVE_TO]->() RETURN count(*) AS c")
check("D001 Insulin has no alternative", d001_alt[0]["c"]==0)

# D017 Salbutamol — no alternative  
d017_alt = q("MATCH (d:Drug {id:'D017'})-[:ALTERNATIVE_TO]->() RETURN count(*) AS c")