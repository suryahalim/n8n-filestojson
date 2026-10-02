import subprocess

def q(sql):
    r = subprocess.run(
        ["docker", "exec", "dp-db", "psql", "-U", "pipeline", "-d", "pipeline", "-Atc", sql],
        capture_output=True, text=True)
    if r.returncode != 0:
        return "ERR: " + r.stderr.strip()[:200]
    return r.stdout.strip()

FOLD = "left(regexp_replace(rel_path, '^[0-9 ]+', ''), 58)"

print("== A. task status ==")
print(q("SELECT status||' = '||count(*) FROM map_tasks GROUP BY status ORDER BY count(*) DESC"))

print("")
print("== B. per-folder coverage ==")
sql = ("WITH f AS (SELECT " + FOLD + " AS folder, document_id, tab_guess, status FROM map_tasks),"
 " rp AS (SELECT document_id, count(*) c FROM faktur_pajak GROUP BY 1),"
 " rf AS (SELECT document_id, count(*) c FROM faktur_penjualan GROUP BY 1),"
 " ro AS (SELECT document_id, count(*) c FROM po_customer GROUP BY 1),"
 " rt AS (SELECT document_id, count(*) c FROM tanda_terima GROUP BY 1) "
 "SELECT f.folder, count(*) || ' docs | ' || count(*) FILTER (WHERE f.status='mapped') || ' mapped | fp=' || "
 "COALESCE(sum(rp.c),0) || ' fps=' || COALESCE(sum(rf.c),0) || ' po=' || COALESCE(sum(ro.c),0) || ' tt=' || COALESCE(sum(rt.c),0) "
 "FROM f LEFT JOIN rp ON rp.document_id=f.document_id LEFT JOIN rf ON rf.document_id=f.document_id "
 "LEFT JOIN ro ON ro.document_id=f.document_id LEFT JOIN rt ON rt.document_id=f.document_id "
 "GROUP BY f.folder ORDER BY min(f.folder)")
for line in q(sql).splitlines():
    print("  ", line)

print("")
print("== C. anomalies ==")
print("non-mapped tasks:")
print(q("SELECT status||' | '||right(rel_path,58) FROM map_tasks WHERE status<>'mapped' ORDER BY 1"))
print("empty issuer in po_customer:")
print(q("SELECT count(*) FROM po_customer WHERE coalesce(po_issuer,'')=''"))
