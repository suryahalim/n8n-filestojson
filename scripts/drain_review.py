#!/usr/bin/env python3
"""Drain review/failed docs through the api engine, one /mapping/run call at a
time (each ~90s). Safe to rerun; stops when queue clean or MAX reached."""
import json, sys, time, urllib.request

BASE = "http://100.68.212.36:5000"
MAX = int(sys.argv[1]) if len(sys.argv) > 1 else 40


def run_batch():
    req = urllib.request.Request(BASE + "/mapping/run",
                                 data=json.dumps({"batch": 1}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read())


done = 0
while done < MAX:
    try:
        out = run_batch()
    except Exception as e:
        print("RUN-ERR", repr(e)[:150], flush=True)
        time.sleep(30)
        continue
    res = out.get("results", [])
    if out.get("ran") == 0:
        print("queue drained", flush=True)
        break
    for r in res:
        done += 1
        line = f"[{done}] {(r.get('document_id') or '')[:8]} tab={r.get('tab')} written={r.get('written')}"
        if r.get("error"):
            line += " ERR=" + str(r["error"])[:120]
        if r.get("note"):
            line += " note=" + str(r["note"])[:80]
        print(line, flush=True)
print("DONE, processed:", done, flush=True)
