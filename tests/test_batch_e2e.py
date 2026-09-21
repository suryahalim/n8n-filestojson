#!/usr/bin/env python3
"""Batch upload E2E (S6/S7): mixed files incl. a duplicate + one re-run.
POST /documents/batch -> each file gets its own document/job/delivery task.
Run: python3 ~/doc-pipeline/tests/test_batch_e2e.py"""
import base64, json, sys, time, urllib.request

EX = "http://100.68.212.36:5000"
FIX = "/home/suryahalim/doc-pipeline/tests/fixtures/"


def post_json(url, obj, timeout=180):
    req = urllib.request.Request(url, data=json.dumps(obj).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def get(url):
    return json.load(urllib.request.urlopen(url, timeout=30))


def b64(name):
    return base64.b64encode(open(FIX + name, "rb").read()).decode()


def make_fresh_copies(tag):
    """New sha per run: xlsx gets an extra row, jpg gets an EXIF comment,
    pdf is regenerated with a unique footer. Keeps S6 deterministic-fresh."""
    import openpyxl, shutil
    from fpdf import FPDF
    n = {}
    # xlsx
    src = FIX + "IT-Asset-Register-PO-8412.xlsx"
    wb = openpyxl.load_workbook(src)
    ws = wb["MaintenanceLog"]
    ws.append([f"2026-09-18", "AST-0112", f"Batch test row {tag}", "Hermes", f"EVID-{tag}.pdf"])
    n["xlsx"] = "/tmp/_batch_asset.xlsx"
    wb.save(n["xlsx"])
    # pdf: small fresh PO page with unique footer
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, f"PURCHASE ORDER PO-2026-8412 (batch copy {tag})", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, "Vendor: PT Bukit Limau Teknologi Informasi  Total: 1,347,873,000 IDR",
             new_x="LMARGIN", new_y="NEXT")
    pdf.output(f"/tmp/_batch_po_{tag}.pdf")
    n["pdf"] = f"/tmp/_batch_po_{tag}.pdf"
    # jpg: copy with slight quality tweak -> different bytes
    from PIL import Image
    im = Image.open(FIX + "SCAN-photo-DN-991.jpg").convert("RGB")
    # Make each run's photo bytes unique so prior test runs cannot poison dedupe.
    px = im.load()
    px[0, 0] = ((px[0, 0][0] + int(tag[-1])) % 256, px[0, 0][1], px[0, 0][2])
    n["jpg"] = "/tmp/_batch_photo.jpg"
    im.save(n["jpg"], quality=88)
    return n


def batch(files, notes):
    """files: list of (display_name, real_path) OR plain fixture names."""
    payload = []
    for f in files:
        name, path = f if isinstance(f, tuple) else (f, FIX + f)
        payload.append({"name": name, "data_b64": base64.b64encode(open(path, "rb").read()).decode()})
    return post_json(f"{EX}/documents/batch", {
        "doc_type": "report", "notes": notes, "skip_existing": True, "files": payload})


def wait_delivered(ids, limit=40):
    out = {}
    for _ in range(limit):
        pending = [i for i in ids if i not in out]
        for i in pending:
            d = get(f"{EX}/documents/{i}")
            if d["status"] in ("DELIVERED", "FLAGGED", "DELIVERY_FAILED"):
                out[i] = d["status"]
        if len(out) == len(ids):
            break
        time.sleep(3)
    return out


def main():
    ok = True
    tag = str(int(time.time()))
    fresh = make_fresh_copies(tag)
    print("[S6] batch of 4: PO.pdf + asset.xlsx + photo.jpg + PO.pdf (duplicate of #1)")
    r = batch([("PO-batch.pdf", fresh["pdf"]), ("asset-batch.xlsx", fresh["xlsx"]),
               ("photo-batch.jpg", fresh["jpg"]), ("PO-batch-copy.pdf", fresh["pdf"])],
              f"S6_batch_{tag}")
    print("    summary:", json.dumps(r["summary"]))
    statuses = {}
    for x in r["results"]:
        statuses.setdefault(x["filename"], []).append(x["status"])
    checks = [
        ("total == 4", r["total"] == 4),
        ("PO.pdf 1x VALIDATED + 1x SKIPPED_DUPLICATE",
            sorted(statuses.get("PO-batch.pdf", []) + statuses.get("PO-batch-copy.pdf", []))
            == ["SKIPPED_DUPLICATE", "VALIDATED"]),
        ("xlsx VALIDATED", "VALIDATED" in statuses.get("asset-batch.xlsx", [])),
        ("jpg FLAGGED (no OCR yet)", "FLAGGED" in statuses.get("photo-batch.jpg", [])),
        ("skipped dup points at existing doc",
            all(x.get("existing_status") for x in r["results"] if x["status"] == "SKIPPED_DUPLICATE")),
    ]

    valid_ids = [x["document_id"] for x in r["results"] if x["status"] in ("VALIDATED", "FLAGGED")]
    print(f"[S6b] tracking {len(valid_ids)} new docs to terminal state...")
    final = wait_delivered(valid_ids)
    checks.append(("2 new docs DELIVERED",
                   sum(1 for v in final.values() if v == "DELIVERED") >= 2))
    print("    final states:", final)

    print("[S7] re-submit the same three files -> all skipped (dedupe by sha256)")
    r2 = batch([("PO-batch.pdf", fresh["pdf"]), ("asset-batch.xlsx", fresh["xlsx"]),
                ("photo-batch.jpg", fresh["jpg"])], "S7_rerun")
    print("    summary:", json.dumps(r2["summary"]))
    checks.append(("S7: all 3 SKIPPED_DUPLICATE",
                   r2["summary"].get("SKIPPED_DUPLICATE") == 3))

    for name, passed in checks:
        print(f"    {'PASS' if passed else 'FAIL'}  {name}")
        ok &= passed
    print("\nRESULT:", "ALL PASS — batch intake + dedupe working ✓" if ok else "SOME CHECKS FAILED ✗")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
