#!/usr/bin/env python3
"""drain_review.py — parallel drain of review/failed docs through the API mapping
engine. N workers (default 4): each atomically claims ONE doc (/mapping/claim is a
conditional UPDATE ... RETURNING, so workers never collide), calls the chat API with
the rules-digest prompt (same as /mapping/run), and POSTs gated rows via the service.

Docs are id-independent; token plan quota is the real limit. Safe to rerun."""
import json, re, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = "http://100.68.212.36:5000"
NWORKERS = int(sys.argv[1]) if len(sys.argv) > 1 else 4
MAX = int(sys.argv[2]) if len(sys.argv) > 2 else 100


def api(path, data=None, method=None, timeout=60):
    req = urllib.request.Request(BASE + path,
                                 data=json.dumps(data).encode() if data else None,
                                 headers={"Content-Type": "application/json"},
                                 method=method or ("POST" if data else "GET"))
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def note(did, status, err):
    api(f"/mapping/{did}/note", {"status": status, "error": err})


def one(_):
    # 1) claim one doc
    pend = api("/mapping/pending?limit=10&statuses=review,failed", timeout=30)
    ids = [t["document_id"] for t in pend["tasks"]]
    if not ids:
        return None  # nothing left
    got = api("/mapping/claim", {"document_ids": ids[:1], "engine": "api"})["claimed"]
    if not got:
        time.sleep(2)
        return "race"
    did = got[0]
    stem = pend["tasks"][0]["rel_path"] or pend["tasks"][0]["filename"]
    # 2) map via LLM (rules digest is in the prompt, built server-side by /mapping/run)
    try:
        out = api("/mapping/run", {"document_id": did}, timeout=500)
    except Exception as e:
        note(did, "review", f"run failed: {str(e)[:120]}")
        return f"ERR {did[:8]} {str(e)[:60]}"
    r0 = (out.get("results") or [{}])[0]
    w = r0.get("written")
    if isinstance(w, dict):
        w = sum(v for v in w.values() if isinstance(v, (int, float)))
    return f"{did[:8]} {str(stem)[-58:]} -> written={w} err={str(r0.get('error') or r0.get('note') or '')[:80]}"


t0 = time.time()
n = 0
with ThreadPoolExecutor(max_workers=NWORKERS) as pool:
    for res in pool.map(one, range(MAX)):
        if res is None:
            break
        n += 1
        print(f"[{time.time()-t0:6.0f}s] {res}", flush=True)
print(f"DONE {n} docs in {(time.time()-t0)/60:.1f} min", flush=True)
