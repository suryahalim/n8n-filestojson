#!/usr/bin/env python3
"""Batch map many documents -> fresh 'Result RPA' copy.

Two document families:
A. e-Faktur digital PDF (DJP export, 1 file = 1 Faktur Pajak)  -> new deterministic parser
B. scan booklet (multi-page, per-vendor parsers from map_sheets) -> reuse build()

Usage:
  python3 batch_map.py --since '2026-09-24 12:00' --until '2026-09-24 12:20' \
      --tag efaktur [more --since/--until/--tag pairs] --dump out.json \
      [COPY_SHEET_ID] [--dry-run]

Writes to a COPY spreadsheet (never the template). Fresh records: clears A:Z, writes
header + data. Deterministic: never invents values; unread -> '' + REVIEW status.
"""
import json, re, sys, os, argparse, collections, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from map_sheets import num, g, build, col_name, HEADERS  # reuse parsers + write helpers

BASE = os.environ.get("EXTRACTOR_URL", "http://100.68.212.36:5000")

# ---------------- A. e-Faktur digital parser ----------------------------------
ID_RE = r'(?:\d{15}|\d{16}|\d{20}|\d{22})'

def fp_line_items(lines):
    """Item blocks in DJP e-Faktur text layout:
        <harga_jual>            (money 88.114,801)
        <nama barang>
        Rp <harga satuan> x <qty>
        [Potongan Harga : Rp <d> [PPN : Rp <p>]]
    Stops at the 'Harga Jual / Penggantian' subtotal footer.
    Returns list of dicts, remainder lines."""
    items = []
    i = 0
    def money(s):
        s = s.strip()
        return num(s) if re.fullmatch(r'\d{1,3}(?:\.\d{3})*(,\d+)?', s) else None
    while i < len(lines):
        line = lines[i].strip()
        m = re.fullmatch(r'Harga Jual / Penggantian\s+[\d.,]+', line)
        if m:
            break
        hj = money(line)
        if hj is not None and i + 2 < len(lines):
            name = lines[i+1].strip()
            m2 = re.match(r'^Rp\s+([\d.,]+)\s*[xX]\s*([\d.,]+)\s*$', lines[i+2].strip())
            if m2 and name and not re.match(r'^\d', name) and 'Potongan' not in name \
               and 'Harga' not in name and 'Dikurangi' not in name and 'Total' not in name:
                it = {"name": name, "harga_jual": hj,
                      "unit_price": num(m2.group(1)), "qty": num(m2.group(2))}
                j = i + 3
                if j < len(lines) and lines[j].strip().startswith('Potongan Harga'):
                    pot = lines[j].strip()
                    it["disc"] = g(pot, r'Potongan Harga\s*:\s*Rp\s*([\d.,]+)')
                    pm = re.search(r'PPN\s*:\s*Rp\s*([\d.,]+)', pot)
                    if pm: it["disc_ppn"] = pm.group(1)
                    j += 1
                items.append(it)
                i = j
                continue
        # variant (PPN-dibebaskan layout): "1.800.000,001 ITEM NAME" then "Rp 180.000 x 10"
        mgl = re.match(r'^([\d.]+,\d{2})(\d{1,3})\s+(\S.*)$', line)
        if mgl and i + 1 < len(lines):
            hj2 = money(mgl.group(1))
            m2b = re.match(r'^Rp\s+([\d.,]+)\s*[xX]\s*([\d.,]+)\s*$', lines[i+1].strip())
            if hj2 is not None and m2b:
                it = {"name": mgl.group(3).strip(), "harga_jual": hj2,
                      "unit_price": num(m2b.group(1)), "qty": num(m2b.group(2))}
                items.append(it)
                i += 2
                continue
        # variant 2: "172.992,001" alone, next line "64828164 NAME", next "Rp x N", next "Potongan Harga : Rp ..."
        mgl2 = re.match(r'^([\d.]+,\d{2})(\d{1,3})$', line)
        if mgl2 and i + 3 < len(lines):
            hj3 = money(mgl2.group(1))
            nm3 = re.match(r'^(\d{5,14})\s+(\S.*)$', lines[i+1].strip())
            m2c = re.match(r'^Rp\s+([\d.,]+)\s*[xX]\s*([\d.,]+)\s*$', lines[i+2].strip())
            if hj3 is not None and nm3 and m2c:
                it = {"name": nm3.group(2).strip(), "harga_jual": hj3, "item_code": nm3.group(1),
                      "unit_price": num(m2c.group(1)), "qty": num(m2c.group(2))}
                j3 = i + 3
                if lines[j3].strip().startswith('Potongan Harga'):
                    pot = lines[j3].strip()
                    it["disc"] = g(pot, r'Potongan Harga\s*:\s*Rp\s*([\d.,]+)')
                    j3 += 1
                items.append(it)
                i = j3
                continue
        i += 1
    return items, lines[i:]

def fp_scan_meta(lines):
    meta = {"npwp": [], "nitku": [], "names": [], "addresses": [], "kode_seri": "", "dpp": "", "ppn": "",
            "ppnbm": "", "harga_jual": "", "pot_huang": "", "uang_muka": "",
            "ppn_dev": "", "date": "", "tebus": ""}
    for ix, line in enumerate(lines):
        s = line.strip()
        if re.match(r'^NPWP\s*:', s) and len(meta["npwp"]) < 2:
            meta["npwp"].append(g(s, r'NPWP\s*:\s*([\d.\-/ ]+)'))
        elif re.match(r'^NITKU\s*:', s) and len(meta["nitku"]) < 2:
            meta["nitku"].append(g(s, r'NITKU\s*:\s*([\d ]+)'))
        elif re.match(r'^Nama\s*:', s) and len(meta["names"]) < 2 and ix+1 < len(lines) and 'Alamat' in lines[ix+1]:
            meta["names"].append(re.sub(r'^Nama\s*:\s*', '', s).strip())
            meta["addresses"].append(re.sub(r'^Alamat\s*:\s*', '', lines[ix+1].strip()).strip())
        elif 'Kode dan Nomor Seri Faktur Pajak' in s:
            meta["kode_seri"] = g(s, r'Seri Faktur Pajak\s*:\s*([\d.\- ]+)')
        meta["dpp"] = g(s, r'^Dasar Pengenaan Pajak\s+([\d.,]+)') or meta["dpp"]
        meta["ppn"] = g(s, r'^Total PPN\s+([\d.,]+)') or meta["ppn"]
        tm = re.match(r'^(\d+)/?(SOR\d+)', s)
        if tm: meta["tebus"] = f"{tm.group(1)}/{tm.group(2)}"
        dm = re.search(r'([A-Z .]+),\s*(\d{1,2} \w+ \d{4})\s*$', s)
        if dm and not meta["date"]: meta["date"] = dm.group(2)
        m1 = re.match(r'^Harga Jual / Pengganti(?:an)?\s+([\d.,]+)', s)
        if m1: meta["harga_jual"] = m1.group(1)
        m2 = re.match(r'^([\d.,]+)\s*Dikurangi Potongan Harga', s)
        if m2: meta["pot_huang"] = m2.group(1)
        m3 = re.match(r'^([\d.,]+)Dikurangi Uang Muka', s)
        if m3: meta["uang_muka"] = m3.group(1)
        m4 = re.match(r'^([\d.,]+)Dikurangi PPN yang harus dibayar dengan DPP Nilai Lain', s)
        if m4: meta["ppn_dev"] = m4.group(1)
    return meta

def efaktur_rows(filename, text):
    """Return (header_row, item_rows) or None if not an e-Faktur digital doc."""
    if not re.search(r'Kode dan Nomor Seri Faktur Pajak', text):
        return None
    seller_npwp = buyer_npwp = ''
    m = re.match(r'^(\d{15})-(\d{16})-(\d{16})-', filename or '')
    if m:
        seller_npwp, kode_seri_from_name, buyer_npwp = m.groups()
    items, rest = fp_line_items(text.splitlines())
    meta = fp_scan_meta(text.splitlines())
    kode_seri = meta["kode_seri"]
    if not kode_seri and m:
        ks = m.group(2)
        kode_seri = f"{ks[:3]}.{ks[3:6]}-{ks[6:8]}.{ks[8:]}"
    # text order: block 1 = PEMBELI (Nama/Alamat/NPWP/NITKU), block 2 = PKP (pengusaha)
    npwp_buyer  = meta["npwp"][0] if len(meta["npwp"]) > 0 else buyer_npwp
    npwp_seller = meta["npwp"][1] if len(meta["npwp"]) > 1 else seller_npwp
    nitku_buyer  = meta["nitku"][0] if len(meta["nitku"]) > 0 else ''
    nitku_seller = meta["nitku"][1] if len(meta["nitku"]) > 1 else ''
    dpp_total = meta["dpp"] or g(text, r'Dasar Pengenaan Pajak\s*\*\s*([\d.,]+)')
    ar = []
    for it in items:
        unit, qty, hj = it.get("unit_price"), it.get("qty"), it.get("harga_jual")
        ar.append('PASS' if (unit is not None and qty not in (None, 0) and hj is not None
                 and abs(hj - unit*qty) <= max(0.01, abs(hj)*0.0005)) else 'REVIEW')
    arith = 'PASS' if items and all(a == 'PASS' for a in ar) else ('MISMATCH' if items else 'NO_ITEMS')
    # cross-check: sum(item harga_jual) vs footer Harga Jual, and DPP+PPN vs total (toleransi potongan)
    def _n(s):
        try: return float(str(s).replace('.','').replace(',','.'))
        except Exception: return None
    tsum = sum(it['harga_jual'] for it in items if it.get('harga_jual') is not None)
    hjs = _n(meta.get('harga_jual')); pot = _n(meta.get('pot_huang')) or 0
    dppv = _n(meta.get('dpp')); ppnv = _n(meta.get('ppn'))
    cross = []
    if items and hjs is not None:
        # per-item values carry sub-cent precision in DJP PDFs; footer rounds to rupiah
        if abs(tsum - pot - (dppv if dppv is not None else hjs - pot)) > max(1.0, hjs*0.0002):
            pass  # DPP relationship varies (PPN tak langsung); informational only
        if abs(tsum - hjs) > max(1.0, hjs*0.0002):
            cross.append(f"item-sum {tsum:,.2f} != footer {hjs:,.2f}")
    if cross: arith = 'REVIEW-CROSS'
    meta_out_cross = '; '.join(cross)[:200]
    header = {
        "kode_seri": kode_seri,
        "npwp_seller": npwp_seller, "nitku_seller": nitku_seller,
        "npwp_buyer": npwp_buyer, "nitku_buyer": nitku_buyer,
        "dpp": dpp_total, "ppn": meta["ppn"], "ppnbm": meta.get("ppnbm") or '',
        "harga_jual": meta.get("harga_jual") or '',
        "pot_huang": meta.get("pot_huang") or '',
        "uang_muka": meta.get("uang_muka") or '',
        "ppn_dev": meta.get("ppn_dev") or '',
        "tgl_transaksi": meta["date"], "tgl_tebus": meta["tebus"],
        "buyer": (meta["names"][0] if meta["names"] else ''),
        "n_items": len(items), "arith": arith, "seller_npwp": seller_npwp, "cross": meta_out_cross,
    }
    return header, items

FP_HEADERS = [
 'SOR','Billing Number','Kode Seri','NPWP & NITKU Pengusaha','Dasar Pengenaan Pajak','PPN','Tanggal Transaksi',
 'NPWP & NITKU Pembeli','Nama Pembeli','Nama Barang Kena Pajak','Qty','Harga Satuan','Jumlah Harga',
 'Potongan Harga','Uang Muka','PPN Dev','PPnBM','Harga Jual Total','Source File','Mapping Status']

def fp_sheet_rows(rows_by_doc):
    """One row per ITEM; faktur header columns repeated (like PO Customer tab). Header-only row if no items."""
    out = []
    for fname, (header, items) in rows_by_doc.items():
        base = [header["tgl_tebus"], header["kode_seri"], header["kode_seri"],
                f'{header["npwp_seller"]} / {header["nitku_seller"]}'.strip(' /'),
                header["dpp"], header["ppn"], header["tgl_transaksi"],
                f'{header["npwp_buyer"]} / {header["nitku_buyer"]}'.strip(' /'),
                header.get("buyer") or '', None, None, None, None,
                header["pot_huang"], header["uang_muka"], header["ppn_dev"], header.get("ppnbm") or '0,00',
                header["harga_jual"], fname, None]
        if not items:
            r = list(base); r[9] = '[NO ITEMS]'; r[19] = 'REVIEW'
            out.append(r)
            continue
        for it in items:
            unit = it.get("unit_price"); qty = it.get("qty"); hj = it.get("harga_jual")
            chk = 'PASS' if (unit is not None and qty not in (None, 0) and hj is not None
                             and abs(hj - unit*qty) <= max(0.01, abs(hj)*0.0005)) else 'REVIEW'
            r = list(base)
            r[9] = it["name"]; r[10] = int(qty) if qty is not None else ''
            r[11] = unit; r[12] = hj
            if 'disc' in it: r[13] = it['disc']
            r[19] = 'MAPPED' if chk == 'PASS' else 'REVIEW'
            r.append(chk)  # per-item arith appended beyond mapping status? no:
            r.pop()
            out.append(r)
    return out

def canon_vendor(n):
    """Normalize OCR'd company names: whitespace, trailing page junk, known typos."""
    n = re.sub(r'\s+', ' ', str(n)).strip().rstrip('.,')
    n = re.sub(r'\s+LEMBAR\s+I\s*$|\s+I\s*$', '', n, flags=re.I)
    n = re.sub(r'Supra\s+Baga\s+Lestari', 'Supra Boga Lestari', n)
    return n.strip()

def fn_is_efaktur(fn):
    return bool(re.match(r'^\d{15}-\d{16}-\d{16}-\d{14}\.pdf$', fn or ''))

# ---------------- fetch --------------------------------------------------------
def fetch_all(since=None, until=None):
    """All docs in window (paginated), each with full standard_json."""
    out = {}
    off = 0
    while True:
        r = urllib.request.urlopen(f"{BASE}/documents?limit=50&offset={off}", timeout=30)
        docs = json.loads(r.read())["documents"]
        if not docs: break
        for d in docs:
            c = d.get("created") or ""
            if since and c < since: continue
            if until and c >= until: continue
            full = json.loads(urllib.request.urlopen(f"{BASE}/documents/{d['id']}", timeout=60).read())
            out[d["id"]] = full
        off += 50
        if off > 4000: break
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--since'); ap.add_argument('--until')
    ap.add_argument('--window', action='append', help='repeatable "SINCE|UNTIL|tag"')
    ap.add_argument('--dump')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('sid', nargs='?')
    a = ap.parse_args()
    windows = [w.split('|') for w in (a.window or [])]
    if a.since: windows.append([a.since, a.until or '', 'docs'])
    # load cached corpus
    cache = '/tmp/batch_map_docs.json'
    if os.path.exists(cache) and '--refresh' not in sys.argv:
        corpus_all = json.load(open(cache))
        corpus = {}
        for s, u, tag in windows:
            for did, d in corpus_all.items():
                c = d.get('created') or ''
                if (not s or c >= s) and (not u or c < u):
                    corpus[did] = d
        print(f'cache: {len(corpus)} docs of {len(corpus_all)}')
    else:
        corpus = {}
        for s, u, tag in windows:
            got = fetch_all(s, u)
            print(tag, len(got))
            corpus.update(got)
        json.dump(corpus, open(cache, 'w'))
        print(f'saved {len(corpus)} docs to cache')

    sheets = collections.defaultdict(list)
    review = []
    fp_rows_by_doc = {}
    fp_ok = fp_low = 0
    skipped_ocr = []
    for did, doc in sorted(corpus.items(), key=lambda kv: kv[1].get('created') or ''):
        std = doc.get('standard_json') or {}
        pages = std.get('pages') or []
        if doc.get('status') == 'FLAGGED' and not fn_is_efaktur(doc['filename']):
            skipped_ocr.append((doc['filename'][:34], f"{len(pages)} pages so far"))
            continue
        text_all = '\n'.join((p.get('text') or '') for p in sorted(pages, key=lambda p: p.get('page', 0)))
        if 'Kode dan Nomor Seri Faktur Pajak' in text_all and re.match(r'^\d{15}-\d{16}-\d{16}-\d{14}\.pdf$', doc['filename'] or ''):
            parsed = efaktur_rows(doc['filename'], text_all)
            if parsed:
                header, items = parsed
                rows = fp_sheet_rows({doc['filename']: parsed})
                if header['arith'] == 'PASS' and rows:
                    sheets['Faktur Pajak'] += rows; fp_ok += 1
                else:
                    sheets['Faktur Pajak'] += rows if rows else [[header['kode_seri']] + ['']*19 + [doc['filename'], 'REVIEW']]
                    fp_low += 1
                review.append([did[:8], 'Faktur Pajak (e-Faktur digital)', 'HIGH' if header['arith']=='PASS' else 'MEDIUM',
                               header['arith'], '', len(text_all), ' '.join(text_all.split())[:300],
                               'MAPPED' if header['arith']=='PASS' else 'REVIEW_REQUIRED'])
                continue
        # scan booklet -> existing per-vendor page classifiers
        sh, rv = build(pages)
        # --- self-company vendor rule (learned): OUR company is not a vendor ---
        rules_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'vendor_rules.json')
        try:
            RULES = json.load(open(rules_path))
        except Exception:
            RULES = {"self_companies": [], "junk": [], "learned_po_vendor": {}}
        self_re = re.compile('|'.join(RULES.get('self_companies') or [r'SARANA\s+ABADI\s+MAKMUR']), re.I)
        junk_re = re.compile('|'.join(RULES.get('junk') or [])) if RULES.get('junk') else None
        comp_re = re.compile(r'(?:PT\.?\s+|CV\.?\s+|PD\.?\s+)([A-Z][A-Za-z .,&\'-]{5,45})')
        page_by_no = {int(p.get('page', 0)): (p.get('text') or '') for p in pages}
        # --- PO number backfill: item parsers may miss the header PO# (e.g. GrandLucky
        # 'PO# : 9919.PL.26.034256', Gramedia 'Purchase Order No. POGAM..'). Fill from the
        # page's own label; continuation pages inherit the nearest previous labeled page
        # (max 3 ahead) ONLY when the vendor matches — never cross-contaminate POs.
        PO_LABS = [
            r'PO[#\s]*[:=]\s*([A-Z0-9][A-Z0-9./_-]{5,27})',
            r'Purchase Order No[.\s]*[:=]?\s+([A-Z0-9][A-Z0-9./_-]{5,27})',
            r'PURCHASE\s+ORDER\s*\n+\s*(\d{6,12})\b',
            r'NO[.\s]*PO\s*[:=]\s*([A-Z0-9][A-Z0-9./_-]{5,27})',
            r'Nomor\s*PO\s*[:=]\s*([A-Z0-9][A-Z0-9./_-]{5,27})',
            r'PO\s*No\.?\s*[:=]\s*([A-Z0-9][A-Z0-9./_-]{5,27})',
            r'PO\s*NUMBER\s*[:=]?\s*([A-Z0-9][A-Z0-9./_-]{5,27})',
            r'ORDER\s*NO\.?\s*[:=]\s*([A-Z0-9][A-Z0-9./_-]{6,27})',
            r'PR\s*No\s*[:=\n]\s*(\d{6,12})',
            r'[:=]\s*(\d{10})\b(?=[^\n]*Purch\.?\s*Grp)',
        ]
        MONTH_RE = re.compile(r'JAN|FEB|MAR|APR|MEI|JUN|JUL|AGU|SEP|OKT|NOV|DES|OCT|DEC', re.I)
        page_po = {}
        for _pno, _t in page_by_no.items():
            for _rx in PO_LABS:
                _hit = ''
                for _mm in re.finditer(_rx, _t, re.I):
                    _v = _mm.group(1).strip().rstrip('.,')
                    if re.search(r'\d{5,}', _v) and not MONTH_RE.search(_v):
                        _hit = _v[:28]; break
                if _hit:
                    page_po[_pno] = _hit; break
        def _po_for_page(pno):
            v = page_po.get(pno)
            if v:
                return v, 'PAGE'
            for q in sorted((q for q in page_po if q < pno), reverse=True):
                if pno - q <= 3:
                    return page_po[q], 'INHERIT'
                break
            return '', ''
        po_bf = po_bi = 0
        for r in sh.get('PO Customer', []):
            if len(r) >= 11 and not str(r[0]).strip() and isinstance(r[9], int) \
               and 'OCR_PAGE_REVIEW' not in str(r[10]):
                pv, how = _po_for_page(r[9])
                if pv:
                    if how == 'INHERIT':
                        # safety: only inherit when this row's vendor appears on the source page
                        _ptxt = page_by_no.get(r[9], '')
                        _vend = str(r[1]).strip()
                        if _vend and _vend[:12].upper().replace('.', '') not in _ptxt.upper().replace('.', ''):
                            continue
                    r[0] = pv
                    r[10] = (r[10] or '') + ('|PO-BACKFILL' if how == 'PAGE' else '|PO-INHERIT')
                    po_bf += how == 'PAGE'; po_bi += how == 'INHERIT'
        if po_bf or po_bi:
            _st = (doc['filename'] or did[:8]).rsplit('.pdf', 1)[0][:24]
            print(f'  PO backfill {_st}: page-label {po_bf} + inherit {po_bi}', flush=True)
        learned = RULES.setdefault('learned_po_vendor', {})
        junk_all = re.compile('|'.join(RULES.get('junk') or [r'(?!)']))
        for r in sh.get('PO Customer', []):
            if len(r) < 10: continue
            if junk_all.search(str(r[1])):
                r[1] = ''   # never keep an OCR-label as vendor name
            if not self_re.search(str(r[1])):
                continue
            po_key = str(r[0]).strip()
            if po_key and learned.get(po_key):
                r[1] = learned[po_key]; r[10] = (r[10] or '') + '|VENDOR-FIXED'; continue
            t = page_by_no.get(r[9], '') if isinstance(r[9], int) else ''
            cands = []
            for m in comp_re.finditer(t):
                n = canon_vendor(m.group(0))
                if self_re.search(n): continue
                if junk_re and junk_re.search(n): continue
                if n not in cands: cands.append(n)
            # prefer a company right after a vendor/supplier/kepada label
            pick = ''
            lm = re.search(r'(?:Vendor|Supplier|Kepada|To)\s*[:.]?\s*((?:PT\.?|CV\.?)\s*[A-Z][A-Za-z .,&\'-]{4,45})', t, re.I)
            if lm and not self_re.search(lm.group(1)) and not (junk_re and junk_re.search(lm.group(1))):
                pick = canon_vendor(lm.group(1))
            elif cands:
                pick = cands[0]
            if pick:
                r[1] = pick
                r[10] = (r[10] or '') + '|VENDOR-FIXED'
                if po_key: learned[po_key] = pick
            else:
                # cannot attribute a real third-party vendor -> blank the self-company
                # (row then only survives tidy if it carries real item data)
                r[1] = ''
                r[10] = (r[10] or '') + '|VENDOR-UNRESOLVED'
        RULES['self_companies'] = list(set(RULES.get('self_companies') or []) | {r'SARANA\s+ABADI\s+MAKMUR'})
        json.dump(RULES, open(rules_path, 'w'), indent=1, ensure_ascii=False)
        # ----------------------------------------------------------------------
        stem = (doc['filename'] or did[:8]).rsplit('.pdf',1)[0][:28]
        SPIDX = {'Faktur Penjualan': 15, 'PO Customer': 9, 'Tanda Terima': 8,
                 'Surat Jalan': 3, 'Dokumen Pelunasan': 12}
        for k, v in sh.items():
            for row in v:
                row = list(row)
                ix = SPIDX.get(k)
                if ix is not None and len(row) > ix:
                    row[ix] = f"p{row[ix]} {stem}"
                sheets[k].append(row)
        for r in rv:
            r = list(r); r[0] = f"p{r[0]} {stem}"
            review.append(r)
    for r in review:
        if r[1] != 'Faktur Pajak (e-Faktur digital)' and r[0] not in [x[0] for x in sheets['Faktur Pajak']]:
            pass  # review rows keep page numbers; Source File distinguishes
    # PO->vendor consistency: one PO number = one vendor. When the memory (or majority)
    # disagrees with a store-outlet name on some page, align to the majority vendor.
    _pv = collections.defaultdict(collections.Counter)
    for r in sheets['PO Customer']:
        if r[0] and r[1]: _pv[r[0]][r[1]] += 1
    _fixed_conf = 0
    for po, ctr in _pv.items():
        if len(ctr) < 2: continue
        win = ctr.most_common(1)[0][0]
        for r in sheets['PO Customer']:
            if r[0] == po and r[1] and r[1] != win:
                r[1] = win; r[10] = (r[10] or '') + '|VENDOR-ALIGNED'; _fixed_conf += 1
    if _fixed_conf: print('PO vendor conflicts aligned:', _fixed_conf, flush=True)

    # --- split glued PO item rows: whole OCR line got crammed into Product Code cell.
    # Find qty/price/total triple at the tail of the Product string; accept ONLY when
    # qty*price ~= total proves the parse (never invent). Remaining tokens -> UON if unit-like.
    def _num_any(s):
        s = s.strip().rstrip('.%').strip()
        if not re.match(r'^\d[\d.,]*$', s):
            return None
        if '.' in s and ',' in s:
            try: return float(s.replace(',', ''))
            except ValueError: return None
        if ',' in s and re.match(r'^\d{1,3}(,\d{3})+(\.\d+)?$', s):
            return float(s.replace(',', ''))
        if ',' in s and re.match(r'^\d+,\d{2}$', s):
            return float(s.replace(',', '.'))
        if '.' in s and re.match(r'^\d{1,3}(\.\d{3})+$', s):
            return float(s.replace('.', ''))
        try: return float(s)
        except ValueError: return None

    UNIT_TOK = r'(?:LNN|LST|LSN|PCS|PC|BOX|CTN|CRT|KRG|BLK|DZN|PAC|SACH|ROLL|GIN|LTR)'
    split_c = 0
    for r in sheets['PO Customer']:
        if len(r) < 11 or not (r[3] or '').strip():
            continue
        if any(str(r[i]).strip() for i in (4, 6, 8)):
            continue  # already split — never overwrite parsed fields
        toks = (r[3] or '').split()
        if len(toks) < 4:
            continue
        tail = toks[-9:]
        i0 = len(toks) - len(tail)
        best = None
        for ii in range(len(tail)):
            if not re.fullmatch(r'\d{1,5}', tail[ii]):
                continue
            q = float(tail[ii])
            if q <= 0:
                continue
            # numeric tail must run to the last token (no words after qty zone)
            if not all(re.match(r'^\d[\d.,%]*$', t) for t in tail[ii:]):
                continue
            for jj in range(ii + 1, len(tail)):
                p = _num_any(tail[jj])
                if not p or p <= 0:
                    continue
                t = _num_any(tail[-1])
                if t and t > 0 and tail[jj] != tail[-1] and abs(q * p - t) <= max(2, t * 0.002):
                    best = (ii, jj); break
            if best:
                break
        if not best:
            continue
        ii, jj = best
        qty, price, total = tail[ii], tail[jj], tail[-1]
        desc = toks[:i0 + ii]
        uon = ''
        for tk in reversed(toks[max(0, i0 + ii - 4):i0 + ii]):
            if re.fullmatch(UNIT_TOK, tk.upper()):
                uon = tk.upper(); desc = [t for t in desc if t != tk]; break
        r[3] = ' '.join(desc).strip()
        r[4], r[5], r[6], r[8] = qty, uon, price, total
        r[10] = (r[10] or 'OCR_ITEM') + '|SPLIT-GLUED'
        split_c += 1
    if split_c:
        print('PO glued-row split:', split_c, flush=True)

    # --- normalize qty/UON per destination table: unit text belongs in UON, not Qty ---
    UNITS = r'(?:CTN|CRT|CT|PCS|PC|BOX|LNN|LSN|LST|KRG|BLK|DZN|PAC|SACH|ROLL|GIN|LTR|BAL)'
    qty_norm = 0
    for r in sheets['PO Customer']:
        if len(r) < 11:
            continue
        q = str(r[4] or '').strip()
        if not q:
            continue
        m = re.fullmatch(rf'(\d+(?:[.,]\d+)?)\s+({UNITS})', q, re.I)
        if not m:
            m = re.fullmatch(rf'(\d+(?:[.,]\d+)?)\s*/\s*({UNITS})', q, re.I)
        if m:
            r[4] = m.group(1)
            if not str(r[5]).strip():
                r[5] = m.group(2).upper()
            qty_norm += 1
    if qty_norm:
        print('PO qty/UON normalized:', qty_norm, flush=True)

    # post-audit: relabel arith-failing PO rows (never silently PASS corrupt numerics)
    def _n2(s):
        s=str(s).strip()
        if not s: return None
        try:
            if re.fullmatch(r'\d{1,3}(\.\d{3})+(,\d+)?',s): return float(s.replace('.','').replace(',','.'))
            if re.fullmatch(r'\d{1,3}(,\d{3})+(\.\d+)?',s): return float(s.replace(',',''))
            if re.fullmatch(r'\d+(,\d+)?',s): return float(s.replace(',','.'))
            return float(s)
        except Exception: return None
    po_fixed=0
    for r in sheets['PO Customer']:
        pq,pu,pd_,pt=_n2(r[4]),_n2(r[6]),_n2(r[7]),_n2(r[8])
        if None in (pq,pu,pt) or r[10]=='OCR_PAGE_REVIEW': continue
        _keep='|'+'|'.join(x for x in str(r[10]).split('|') if x.startswith(('PO-','SPLIT-'))) if '|' in str(r[10]) else ''
        if abs(pu*pq-(pd_ or 0)-pt)<=max(1.0,abs(pt)*0.001):
            r[10]='MAPPED'+_keep
        else:
            r[10]='REVIEW-ARITH'+_keep; po_fixed+=1
    fp_fixed=0
    for r in sheets['Faktur Penjualan']:
        if r[17]=='SUMMARY': continue
        qm=re.match(r'^\s*(\d+)\s*/\s*(\d+)\s*$', str(r[4]))
        fq=_n2(qm.group(1)) if qm else _n2(r[4])
        pcs=_n2(qm.group(2)) if qm else None
        fp,fa=_n2(r[5]),_n2(r[11])
        discs=sum(_n2(x) or 0 for x in r[6:11])
        if None in (fq,fp,fa) or r[17]=='SUMMARY': continue
        pk=re.match(r'^\s*(\d+)\s*[Xx]\s*\d', str(r[2] or ''))
        pack=int(pk.group(1)) if pk else 1
        cands=[fq*(pack or 1), fq, pcs]
        ok=any(abs(fp*c-discs-fa)<=max(1.0,abs(fa)*0.001) for c in cands)
        if ok: r[17]='PASS'
        else: r[17]='REVIEW-ARITH'; fp_fixed+=1
    print('arith relabel: PO',po_fixed,'FP',fp_fixed, flush=True)

    # --- self-learning variant KB (llm-wiki pattern): apply learned semantics ---
    try:
        import knowledge as KB
        _pl = KB.build_page_lookup(corpus)
        sem_c, corr_c = KB.apply_to_sheets(dict(sheets), _pl)
        print('KB apply: sem-proven', sem_c, '| ocr-corrected', corr_c, flush=True)
        # TT date repair: header text bleeding into Posting Date -> real date from raw page
        ttf = 0
        for r in sheets.get('Tanda Terima', []):
            if len(r) > 9 and r[0] and not re.match(r'^[\d./]', str(r[0])):
                mm = re.match(r'^p(\d+) (.*)$', r[8] or '')
                d = ''
                if mm:
                    for l in _pl.get(mm.group(2).strip(), {}).get(int(mm.group(1)), []):
                        if r[1] and str(r[1]) in l:
                            dm = re.search(r'\d{2}[/.]\d{2}[/.]\d{4}', l)
                            if dm: d = dm.group(0); break
                r[0] = d; ttf += 1
        # normalize valid-but-varied TT date formats (15-SEP-26 / 16/09/26 09:36:53)
        MON = {m: f'{i+1:02d}' for i, m in enumerate('JAN FEB MAR APR MEI JUN JUL AGU SEP OKT NOV DES'.split())}
        for r in sheets.get('Tanda Terima', []):
            if len(r) > 9 and r[0]:
                s = str(r[0]).upper()
                m1 = re.match(r'^(\d{2})-([A-Z]{3})-(\d{2})$', s)
                m2 = re.match(r'^(\d{2})[/.](\d{2})[/.](\d{2})(\s.*)?$', s)
                if m1 and m1.group(2) in MON:
                    r[0] = f'{m1.group(1)}/{MON[m1.group(2)]}/20{m1.group(3)}'
                elif m2:
                    r[0] = f'{m2.group(1)}/{m2.group(2)}/20{m2.group(3)}'
        print('TT date-repair:', ttf, flush=True)
    except Exception as e:
        print('KB apply skipped:', repr(e), flush=True)

    # TIDY: never write placeholder/semantics-less rows to data tabs.
    # - rows whose Mapping/Review status is OCR_PAGE_REVIEW (page-level header leftovers)
    # - rows where every non-provenance cell is empty
    PROV={'Source Page','Mapping Status','Confidence','Review Status','Source File'}
    def _tidy(tab, rows):
        hdr=HEADERS.get(tab, [])
        keepidx=[i for i,h in enumerate(hdr) if h not in PROV]
        out=0
        def good(r):
            if 'OCR_PAGE_REVIEW' in [str(x) for x in r[-2:]]: return False
            if keepidx and not any(str(r[i]).strip() for i in keepidx if i<len(r)): return False
            return True
        kept=[r for r in rows if good(r)]
        return kept
    for k in ['PO Customer','Tanda Terima','Surat Jalan']:
        if k in sheets:
            before=len(sheets[k]); sheets[k]=_tidy(k, sheets[k])
            print('tidy',k,before,'->',len(sheets[k]), flush=True)
    # PO Customer: a row needs a PO number OR item data to mean anything
    before=len(sheets['PO Customer'])
    sheets['PO Customer']=[r for r in sheets['PO Customer']
        if str(r[0]).strip() or any(str(r[i]).strip() for i in range(3,9))]
    if len(sheets['PO Customer'])!=before:
        print('tidy PO Customer (no-PO-no-item) ->',len(sheets['PO Customer']), flush=True)
    # Faktur Penjualan: drop rows where all data cols empty (confidence/review status only)
    sheets['Faktur Penjualan']=[r for r in sheets['Faktur Penjualan']
        if any(str(r[i]).strip() for i in range(15))]

    counts = {k: len(v) for k, v in sheets.items() if k != 'Faktur Pajak'}
    counts['Faktur Pajak'] = len(sheets['Faktur Pajak'])
    counts['Faktur Pajak (docs)'] = f'{fp_ok} PASS / {fp_low} REVIEW'
    counts['OCR Mapping Review'] = len(review)
    counts['skipped-OCR-incomplete'] = skipped_ocr
    if a.dump:
        json.dump({'sheets': dict(sheets), 'review': review, 'counts': counts},
                  open(a.dump, 'w'), ensure_ascii=False, indent=1)
    print(json.dumps(counts, ensure_ascii=False, indent=1))
    if not a.dry_run and a.sid:
        from map_sheets import write as _ws
        # extend write to include custom headers: inject
        HEADERS['Faktur Pajak'] = FP_HEADERS
        print('written:', json.dumps(_ws(a.sid, dict(sheets), review), ensure_ascii=False))
        # --- record PO-number label variants per company (self-learning registry) ---
        try:
            import knowledge as KBV
            KBV.record_po_variants(dict(sheets))
        except Exception as e:
            print('PO variants record skipped:', repr(e), flush=True)
        # --- learn from what we just successfully mapped, then refresh wiki ---
        try:
            import knowledge as KB
            _pl = KB.build_page_lookup(corpus)
            _kb, _prom = KB.learn_from_sheets(dict(sheets), _pl)
            KB.render_wiki(_kb)
            print(f'KB learn: variants={len(_kb["variants"])} promoted={_prom}', flush=True)
        except Exception as e:
            print('KB learn skipped:', repr(e), flush=True)

if __name__ == '__main__':
    main()
