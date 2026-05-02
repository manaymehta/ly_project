import sqlite3, json, glob, os

dbs = glob.glob("*.db")
print(f"DB files found ({len(dbs)}): {dbs}\n")

for db_file in sorted(dbs):
    size = os.path.getsize(db_file) // 1024
    conn = sqlite3.connect(db_file)
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    for table in tables:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        statuses = dict(conn.execute(f"SELECT status, COUNT(*) FROM {table} GROUP BY status").fetchall())
        risks = dict(conn.execute(f"SELECT overall_risk_level, COUNT(*) FROM {table} GROUP BY overall_risk_level").fetchall())
        print(f"{'='*55}")
        print(f"  FILE : {db_file}  ({size} KB)")
        print(f"  TABLE: {table}  rows={count}")
        print(f"  STATUSES : {statuses}")
        print(f"  RISK DIST: {risks}")
        print(f"  COLUMNS  : {cols}")

        # Show one full row in detail
        row = conn.execute(f"SELECT * FROM {table} WHERE overall_risk_level IN ('HIGH_RISK','MEDIUM_RISK') LIMIT 1").fetchone()
        if row:
            rd = dict(zip(cols, row))
            pkg = json.loads(rd.pop("full_package"))
            print(f"\n  --- SAMPLE PACKAGE (non-monitor) ---")
            print(f"  {rd}")
            print(f"\n  full_package top keys   : {list(pkg.keys())}")
            print(f"  disruption              : {pkg.get('disruption')}")
            print(f"  drug keys               : {list((pkg.get('drug') or {}).keys())}")
            hc = pkg.get("hospital_coverage") or []
            print(f"  hospital_coverage count : {len(hc)}")
            print(f"  hospital_coverage[0]    : {hc[0] if hc else 'n/a'}")
            proc = pkg.get("procurement") or {}
            print(f"  procurement keys        : {list(proc.keys())}")
            opt_a = proc.get("option_a") or []
            print(f"  option_a[0]             : {opt_a[0] if opt_a else 'n/a'}")
            clin = pkg.get("clinical") or {}
            print(f"  clinical keys           : {list(clin.keys())}")
        print()
    conn.close()
