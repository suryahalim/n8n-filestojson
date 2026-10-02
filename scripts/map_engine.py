#!/usr/bin/env python3
"""map_engine.py — 'hermes' mapping engine client for the service queue.

Flow: pull /mapping/pending (docs already OCR'd) -> map each with the proven
deterministic parsers (map_sheets.build + po_v2 13-col chain) -> convert sheet
rows to target-table dicts -> POST /documents/{id}/mapping/rows. The SERVER
normalizes types and enforces the rules gate (product name, SAMB-never-issuer,
PPN 11%/1.1%, arithmetic) before writing — the engine cannot sneak values in.

Usage:
  python3 scripts/map_engine.py --folder 'Complete bundles%' [--limit 200] [--dry-run]
  python3 scripts/map_engine.py --ids id1,id2
"""
import argparse, collections, json, os, re, sys, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
BASE = os.environ.get("EX", "http://100.68.212.36:5000")

from map_sheets import build                      # page classifiers -> legacy 12-col rows
from batch_map import efaktur_rows, fp_sheet_rows # e-Faktur (digital + scan meta)
import po_v2 as PO2

SHEET_TO_COLS = {  # sheet row index -> target-table column name
    "faktur_pajak": dict(enumerate(["sor", "billing_number", "kode_seri", "npwp_pengusaha",
        "dasar_pengenaan_pajak", "ppn", "tanggal_transaksi", "npwp_pembeli", "nama_pembeli",
        "nama_bkp", "qty", "harga_satuan", "jumlah_harga", "potongan_harga", "uang_muka",
        "ppn_dev", "ppnbm", "harga_jual_total", "source_file", "mapping_status"])),
    "faktur_penjualan": dict(enumerate(["kode_material", "sor", "kemasan", "nama_produk", "qty",
        "harga", "disc_1", "disc_2", "disc_3", "disc_4", "disc_5", "jumlah",
        "dasar_pengenaan_pajak", "ppn", "total", "source_page", "confidence", "review_status"])),
    "po_customer": dict(enumerate(["purchase_order_no", "vendor_code", "po_issuer", "ppn",
        "product_code", "product_name", "qty", "uon", "unit_price", "discount", "total",
        "source_page", "mapping_status"])),
    "tanda_terima": dict(enumerate(["posting_date", "document_no", "purchase_order_no",
        "vendor_number", "item_code", "material_description", "qty", "uon",
        "source_page", "mapping_status"])),
}
TAB_BY_SHEET = {"Faktur Pajak": "faktur_pajak", "Faktur Penjualan": "faktur_penjualan",
                "PO Customer": "po_customer", "Tanda Terima": "tanda_terima"}

PO_LABS = [r'PO[#\s]*[:=]\s*([A-Z0-9][A-Z0-9./_-]{5,27})',
           r'Purchase Order No[\.\s]*[:=]?\s+([A-Z0-9][A-Z0-9./_-]{5,27})',
           r'NO[\.\s]*PO\s*[:=]\s*([A-Z0-9][A-Z0-9./_-]{5,27})',
           r'Nomor\s*PO\s*[:=]\s*([A-Z0-9][A-Z0-9./_-]{5,27})',
           r'ORDER\s*NO\.?\s*[:=]\s*([A-Z0-9][A-Z0-9./_-]{6,27})']
MONTH_RE = re.compile(r'JAN|FEB|MAR|APR|MEI|JUN|JUL|AGU|SEP|OKT|NOV|DES|OCT|DEC', re.I)


def api(path, body=None, method=None, timeout=90):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method or ("POST" if data else "GET"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def po_backfill(rows, page_by_no):
    """Per-page PO# label + inherit (max 3 pages, issuer must appear on page) — same
    rules as batch_map. Rows are 13-col lists."""
    page_po = {}
    for pno, t in page_by_no.items():
        for rx in PO_LABS:
            for mm in re.finditer(rx, t, re.I):
                v = mm.group(1).strip().rstrip('.,')
                if re.search(r'\d{5,}', v) and not MONTH_RE.search(v):
                    page_po[pno] = v[:28]; break
            if pno in page_po: break
    for r in rows:
        if len(r) >= 13 and not str(r[0]).strip() and isinstance(r[11], int):
            pv, how = page_po.get(r[11]), None
            if not pv:
                prev = sorted((q for q in page_po if q < r[11]), reverse=True)
                if prev and r[11] - prev[0] <= 3:
                    pv, how = page_po[prev[0]], 'INHERIT'
            if pv:
                if how == 'INHERIT':
                    core = re.sub(r'[^A-Z0-9]', '', (str(r[1]) + ' ' + str(r[2])).upper())[:10]
                    if core and core not in re.sub(r'[^A-Z0-9]', '', page_by_no.get(r[11], '').upper()):
                        continue
                r[0] = pv
                r[12] = (r[12] or '') + ('|PO-BACKFILL' if how is None else '|PO-INHERIT')
    return rows


def map_document(did):
    """Returns (tab -> list-of-dict-rows, review_notes list)."""
    d = api(f"/documents/{did}")
    std = d.get("standard_json") or {}
    pages = sorted(std.get("pages") or [], key=lambda p: p.get("page", 0))
    text_all = "\n".join((p.get("text") or "") for p in pages)
    out = collections.defaultdict(list)
    notes = []
    # 1) e-Faktur (digital PDF text layer) — content-detected, not filename
    if "Kode dan Nomor Seri Faktur Pajak" in text_all:
        parsed = efaktur_rows(d.get("filename"), text_all)
        if parsed:
            for r in fp_sheet_rows({(d.get("filename") or did[:8]): parsed}):
                m = {}
                for i, col in SHEET_TO_COLS["faktur_pajak"].items():
                    if i < len(r):
                        m[col] = r[i]
                m.pop("mapping_status", None)
                m["review_status"] = r[19] if len(r) > 19 else None
                out["faktur_pajak"].append(m)
    # 2) page classifiers (all tabs)
    sh, rv = build(pages)
    page_by_no = {int(p.get("page", 0)): (p.get("text") or "") for p in pages}
    # 3) PO v2 13-col: adapt old rows + chain on uncovered PO pages (same as batch_map)
    _po13, _cov = [], set()
    for r in sh.get("PO Customer", []):
        try: pno = int(r[9])
        except (ValueError, TypeError): pno = None
        ar = PO2.adapt_old(list(r), page_by_no.get(pno or 0, ""), r[9])
        _po13.append(ar)
        if pno: _cov.add(pno)
    for p in pages:
        pno = int(p.get("page", 0))
        if pno in _cov: continue
        t = p.get("text") or ""
        if "PURCHASE ORDER" not in t.upper() and not re.search(r'NO PO\s*:|PO ?Number|PO#|Nomor P?\.?O', t):
            continue
        try:
            _new = PO2.build_page(t, pno)
        except Exception:
            _new = []
        if _new:
            _po13 = [x for x in _po13 if not (isinstance(x[11], int) and x[11] == pno
                                              and 'PURCHASE ORDER' not in str(x[5] or '').upper() and x[12] == 'OCR_PAGE_REVIEW')]
            _po13 += _new
    if _po13:
        sh["PO Customer"] = po_backfill(_po13, page_by_no)
    # 4) tidy: product-name rule + junk-name filter (mirrors batch_map; server also gates)
    if sh.get("PO Customer"):
        JUNK = re.compile(r'^[-–—]?\s*(?:Jumlah|Kekurangan|Selisih|Sisa|Printed|Halaman|Total\b|Sub\s*Total)', re.I)
        sh["PO Customer"] = [r for r in sh["PO Customer"]
                             if len(r) > 5 and str(r[5]).strip() and not JUNK.match(str(r[5]))]
    # 5) convert sheet rows -> dicts
    for sheet_tab, rows in sh.items():
        tab = TAB_BY_SHEET.get(sheet_tab)
        if not tab or tab == "faktur_pajak" and out["faktur_pajak"]:
            continue
        for r in rows:
            r = list(r)
            if tab == "faktur_penjualan":
                r = r[:15] + [f"p{r[15]}" if len(r) > 15 else "p0"] + r[16:]
            m = {}
            for i, col in SHEET_TO_COLS[tab].items():
                if i < len(r):
                    m[col] = r[i]
            if tab == "faktur_pajak":
                m.pop("mapping_status", None)
                m["review_status"] = r[19] if len(r) > 19 else None
            if not any(str(v).strip() for k, v in m.items() if k not in
                       ("source_page", "source_file", "mapping_status", "confidence", "review_status")):
                continue
            out[tab].append(m)
    if rv:
        notes.append(f"{len(rv)} review page(s)")
    if not out and not notes:
        notes.append("no rows from any parser")
    return d, out, notes


def post_rows(did, tab, rows, dry=False):
    if dry:
        return {"dry": True, "written": len(rows)}
    return api(f"/documents/{did}/mapping/rows", {"tab": tab, "rows": rows})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--folder"); ap.add_argument("--ids")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--engine", default="hermes")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    pend = api(f"/mapping/pending?limit={a.limit}" + (f"&folder={urllib.parse.quote(a.folder)}" if a.folder else ""))
    if a.ids:
        pend["tasks"] = [{"document_id": i} for i in a.ids.split(",")]
    ids = [t["document_id"] for t in pend["tasks"]]
    print(f"pending: {len(ids)} docs")
    if not a.dry_run and ids:
        api("/mapping/claim", {"document_ids": ids, "engine": a.engine})
    tot = collections.Counter()
    for did in ids:
        try:
            d, out, notes = map_document(did)
        except Exception as e:
            print("  ERR", did[:8], repr(e)[:120]); api(f"/mapping/{did}/note", {"status": "failed", "error": str(e)[:300]}); continue
        wres = {}
        for tab, rows in out.items():
            if not rows: continue
            try:
                wres[tab] = post_rows(did, tab, rows, a.dry_run)
            except Exception as e:
                err = str(e)[:160]
                wres[tab] = {"error": err}
                print("   POST-ERR", tab, err[:90])
        n = sum(len(rows) for rows in out.values())
        stem = (d.get("rel_path") or d.get("filename") or did[:8])
        print(f"  {stem[-70:]} -> " + (", ".join(f"{t}:{len(r)}" for t, r in out.items()) or "0 rows"))
        if not out:
            api(f"/mapping/{did}/note", {"status": "review", "error": "; ".join(notes)}) if not a.dry_run else None
        for t, r in out.items(): tot[t] += len(r)
        tot["_docs"] += 1
    print("TOTAL:", dict(tot))


if __name__ == "__main__":
    import urllib.parse
    main()
