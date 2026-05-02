import pandas as pd
from neo4j import GraphDatabase

NEO4J_URI      = "neo4j://127.0.0.1:7687"
NEO4J_USER     = "neo4j"
NEO4J_PASSWORD = "QWEasd123"

def fix_shared_api_risk():
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    df = pd.read_csv("datasets/alt_drug_map.csv", encoding="latin-1")
    df = df.dropna(subset=["Alternative Drug ID"])
    
    with driver.session() as session:
        for _, row in df.iterrows():
            did = str(row["Drug ID"]).strip()
            alt = str(row["Alternative Drug ID"]).strip()
            shared_str = str(row["Shared API Risk"]).strip().lower()
            
            is_shared = True if shared_str == "yes" else False
            
            session.run("""
                MATCH (d:Drug {id: $did})-[r:ALTERNATIVE_TO]->(a:Drug {id: $alt})
                SET r.sharedApiRisk = $val
            """, {"did": did, "alt": alt, "val": is_shared})
            
    print("Graph updated! Fixed sharedApiRisk for all alternatives.")
    driver.close()

if __name__ == "__main__":
    fix_shared_api_risk()
