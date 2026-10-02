import os, json, urllib.request, urllib.error, time
HOST = "http://" + "100.68.212.36"
B = HOST + ":5000"
REAL_KEY = os.environ.get("_DUMMY","")  # full-key leak assertion target; set via env if testing against a live .env

def req(method, path, body=None, timeout=90):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(B + path, data=data, method=method,
                               headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read() or b"{}")
        except Exception: return e.code, {}

ok = True
print("1. settings page fields ...")
html = urllib.request.urlopen(B + "/view/settings", timeout=15).read().decode()
for fld in ["OCR Engine Settings", "Test connection", 'id="model"', "id=\"endpoint\"", "Fetch models"]:
    single = fld.replace('"', "'")
    if fld not in html and single not in html:
        print("   MISSING:", fld); ok = False
print("   fields present:", ok)

print("2. GET /settings/ocr bootstrap ...")
s, cfg = req("GET", "/settings/ocr")
print("  ", s, "model:", cfg["model"], "engine:", cfg["engine"], "source:", cfg["source"])
print("   endpoint set:", bool(cfg["endpoint"]), "masked:", cfg["key_masked"])
print("   real key in response:", "LEAK!!" if REAL_KEY in json.dumps(cfg) else "NO ok")

print("3. fetch models ...")
s, d = req("POST", "/settings/ocr/models", {})
ms = d.get("models", [])
print("  ", s, len(ms), "models; vl/flash:", [m for m in ms if "vl" in m or "flash" in m][:6])

print("4. draft test BAD model ...")
s, d = req("POST", "/settings/ocr/test", {"model": "this-model-does-not-exist"})
print("  ", s, str(d.get("detail"))[:100])
assert s == 502 and "404" in str(d), "expected 502/404 hint"

print("5. draft test GOOD model ...")
s, d = req("POST", "/settings/ocr/test", {"model": "qwen3.8-flash"})
print("  ", s, d)

print("6. SAVE bogus model WITHOUT force -> must be blocked by test gate ...")
s, d = req("POST", "/settings/ocr", {"model": "bogus-model-xyz", "endpoint": cfg["endpoint"], "engine": "qwen"})
print("  ", s, str(d)[:110])
assert s != 200, "gate failed — saved untested config!"

print("7. SAVE real model qwen3.8-flash (passes gate, no force) ...")
s, d = req("POST", "/settings/ocr", {"model": "qwen3.8-flash", "endpoint": cfg["endpoint"], "engine": "qwen"})
print("  ", s, d.get("config", d))

print("8. hot-reload: config file now rules (source=file, NO restart since step 6-7) ...")
s, c2 = req("GET", "/settings/ocr")
print("  ", "source:", c2["source"], "model:", c2["model"])

print("9. E2E extractor-native with config from UI: upload photo + OCR via /documents/{id}/ocr (no engine in payload -> default qwen) ...")
raw = open('/home/suryahalim/doc-pipeline/tests/fixtures/SCAN-photo-DN-991.jpg', 'rb').read()
body = json.dumps({"doc_type": "delivery_order", "notes": "settings-ui e2e",
    "files": [{"name": "p.jpg", "mime": "image/jpeg", "data_b64": __import__('base64').b64encode(raw).decode()}]}).encode()
r = urllib.request.Request(B + "/documents/batch", data=body, method="POST", headers={"Content-Type": "application/json"})
doc_id = json.load(urllib.request.urlopen(r, timeout=120))["results"][0]["document_id"]
t0 = time.time()
b2 = json.dumps({}).encode()
r2 = urllib.request.Request(B + f"/documents/{doc_id}/ocr", data=b2, method="POST", headers={"Content-Type": "application/json"})
res = json.load(urllib.request.urlopen(r2, timeout=300))
print("   %.1fs" % (time.time()-t0), "engine:", res.get("engine"), "chars:", res.get("chars") or res.get("completed_chars"), "validated:", res.get("validated"))
s3, doc = req("GET", "/documents/" + doc_id)
m = doc.get("standard_json", {})
print("   ocr_model recorded:", m.get("ocr_model"), "| status:", doc.get("status"))
assert res.get("validated") and m.get("ocr_model") == "qwen3.8-flash"

print("10. cleanup test doc ...")
q = ("DELETE FROM delivery_tasks WHERE document_id='%s'; "
     "DELETE FROM jobs WHERE document_id='%s'; "
     "DELETE FROM documents WHERE id='%s';") % (doc_id, doc_id, doc_id)
import subprocess
subprocess.run(['docker','exec','dp-db','psql','-U','pipeline','-d','pipeline','-c',q],capture_output=True)
print("\nALL PASS" if ok else "CHECK FAILURES ABOVE")
