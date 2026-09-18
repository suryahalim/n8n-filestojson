#!/usr/bin/env python3
"""No-browser E2E test: upload PDF -> wait delivery -> assert receiving API got
the parsed document as JSON. Run: python3 ~/doc-pipeline/tests/test_api_e2e.py [file.pdf]"""
import json, sys, time, urllib.request, uuid

EX = "http://100.68.212.36:5000"
path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/table_test.pdf"


def post_multipart(url, fname, data, ctype="application/pdf"):
    b = uuid.uuid4().hex
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"{fname}\"\r\nContent-Type: {ctype}\r\n\r\n").encode() \
           + data + f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    return json.load(urllib.request.urlopen(req, timeout=60))


def get(url):
    return json.load(urllib.request.urlopen(url, timeout=30))


def main():
    ok = True
    print(f"[1] uploading {path}")
    resp = post_multipart(f"{EX}/documents", path.split("/")[-1], open(path, "rb").read())
    did = resp["document_id"]
    print(f"    document_id={did} status={resp['status']}")
    assert resp["status"] == "VALIDATED", f"expected VALIDATED, got {resp['status']} (is the PDF text-based?)"

    print("[2] waiting for worker delivery...")
    doc = None
    for _ in range(10):
        time.sleep(3)
        doc = get(f"{EX}/documents/{did}")
        if doc["delivery_tasks"] and doc["delivery_tasks"][0]["status"] == "DELIVERED":
            break
    assert doc and doc["status"] == "DELIVERED", f"doc not delivered: {doc and doc['status']}"
    print("    doc status = DELIVERED")

    print("[3] checking what the RECEIVING API actually got (inbox receipts)")
    inbox = get(f"{EX}/mock/receiving/inbox?limit=3")
    receipt = next((r for r in inbox["receipts"] if r["payload"].get("document_id") == did), None)
    assert receipt, "no inbox receipt for this document!"
    payload = receipt["payload"]
    print(json.dumps(payload, indent=2)[:1200])

    # assertions: the parsed content must be inside the delivered JSON
    ext = payload["extracted"]
    text = json.dumps(ext)
    tokens = sys.argv[2].split(",") if len(sys.argv) > 2 else []
    print("\n[4] asserting parsed content arrived intact")
    checks = [
        ("payload has task_id", "task_id" in payload),
        ("payload has document_id == uploaded doc", payload.get("document_id") == did),
        ("extracted kind is pdf", ext.get("kind") == "pdf"),
        ("extracted has pages array", isinstance(ext.get("pages"), list) and len(ext["pages"]) > 0),
    ] + [(f"expected token present: {t.strip()}", t.strip() in text) for t in tokens]
    for name, passed in checks:
        print(f"    {'PASS' if passed else 'FAIL'}  {name}")
        ok &= passed

    print("\nRESULT:", "ALL PASS — receiving system got the parsed PDF as JSON ✓" if ok else "SOME CHECKS FAILED ✗")
    print(f"Track record anytime: {EX}/documents/{did}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
