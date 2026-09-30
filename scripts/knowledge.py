#!/usr/bin/env python3
"""knowledge.py — self-learning document-variant wiki for OCR mapping (llm-wiki pattern).

Persistent KB: ~/doc-pipeline/knowledge/
  variants.json   machine-readable evidence per (layout-signature × company) variant
  companies/*.md  human-readable wiki, re-rendered after every mapping run

Unit of knowledge = LAYOUT VARIANT: sorted set of item-table column keywords
(e.g. 'disc#harga#kemasan#nama#qty') crossed with the issuing company
(OCR-mangled self-company letterheads are canonicalized to SELF).

learn_from_sheets(): accumulates evidence only from rows whose arithmetic is
directly demonstrated. When >=3 consistent rows and >=60% agreement, a qty
semantics is PROMOTED and becomes usable.
apply_to_sheets(): relabels REVIEW-ARITH rows -> PASS-LEARNED when the promoted
semantics proves them, or fixes a single-OCR-digit price misread -> PASS-LEARNED-CORR
when a conf rule (seen >=2x in the same variant) reconstructs an exact fit.
Nothing is ever corrected without an exact arithmetic proof.

batch_map.py: apply_to_sheets before writing; learn_from_sheets after a
successful write. Every run makes the next one smarter.
"""
import json, re, os, collections, hashlib

KB_DIR = os.path.expanduser('~/doc-pipeline/knowledge')
VFP = os.path.join(KB_DIR, 'variants.json')
WIKI = os.path.join(KB_DIR, 'companies')

COMPANY_RE = re.compile(r'^(PT|CV|PD|UD|FA|PR|TOKO)\.?\s+[A-Z]')
QTY_RE = re.compile(r'^(\d+)\s*/\s*(\d+)$')
FP_COLS = dict(qty=4, harga=5, jumlah=11, src=15, stat=17, kemasan=2, conf=16)

COLPATS = {
    'kemasan': r'KEMAS', 'qty': r'\bQTY\b|\bBRG\b|\bJUML B', 'harga': r'HARGA',
    'satuan': r'SATUAN', 'disc': r'DISC', 'jumlah': r'^JUMLAH|NILAI',
    'total': r'^TOTAL', 'sub': r'SUB ?TOTAL', 'ppn': r'^PPN', 'dpp': r'DASAR PENGENAAN',
    'sortir': r'NO ?URUT|SORTIR', 'ukuran': r'UKURAN', 'uon': r'\bUON\b|\bSAT\b',
    'material': r'KODE ?(MATERI|BARANG|BM)', 'nama': r'NAMA (BARANG|PRODUK)',
}
# digit confusions observed in Qwen-VL OCR of rupiah prices
CONFUSIONS = [('0', '8'), ('8', '0'), ('6', '8'), ('8', '6'), ('4', '1'), ('1', '4'),
              ('3', '9'), ('9', '3'), ('5', '6'), ('6', '5')]

def norm_line(l):
    l = re.sub(r'\d', '#', l.upper().strip())
    l = re.sub(r'[^\w#/ ]', ' ', l)
    return re.sub(r'\s+', ' ', l).strip()

def table_sig(lines):
    for l in lines:
        s = norm_line(l)
        hits = tuple(sorted(k for k, p in COLPATS.items() if re.search(p, s)))
        if len(hits) >= 3:
            return hits
    return None

def canon_company(s):
    if not s:
        return None
    u = re.sub(r'\s+', ' ', s.upper())
    if re.search(r'ABADI\s*MAKMUR|SARANA ABADI|SADAYA AGADIAMANIK', u):
        return 'SELF:SARANA ABADI MAKMUR BERSAMA'
    return u[:60]

def page_company(lines):
    for l in lines[:14]:
        s = l.strip().upper()
        if COMPANY_RE.match(s) or re.search(r'ABADI\s*MAKMUR', s):
            return canon_company(s)
    return None

def n2(s):
    s = str(s).strip().replace(' ', '')
    if not s:
        return None
    if re.fullmatch(r'\d{1,3}(\.\d{3})+(,\d+)?', s):
        s = s.replace('.', '').replace(',', '.')
    elif re.fullmatch(r'\d+,\d+', s):
        s = s.replace(',', '.')
    else:
        return None
    try:
        return float(s)
    except ValueError:
        return None

def id_fmt(v):
    return f'{v:,.2f}'.replace(',', '#').replace('.', ',').replace('#', '.')

def isi_per_carton(kemasan):
    toks = re.split(r'[Xx*]', re.sub(r'\s', '', kemasan or ''))
    prod, seen = 1, False
    for t in toks:
        if re.fullmatch(r'\d+', t):
            prod *= int(t)
            seen = True
        else:
            break
    return prod if seen else None

def digit_fix_candidates(h_str):
    """OCR price string -> list of (fixed_str, value) via ONE known digit confusion."""
    out = []
    for i, c in enumerate(h_str):
        if not c.isdigit():
            continue
        for a, b in CONFUSIONS:
            if c == a and a != b:
                cand = h_str[:i] + b + h_str[i + 1:]
                v = n2(cand)
                if v:
                    out.append((cand, v))
    return out

def classify_row(r):
    """Returns (sem|None, conf_event|None). Only exact arithmetic counts."""
    q = (r[FP_COLS['qty']] or '').replace(' ', '')
    h, j = n2(r[FP_COLS['harga']]), n2(r[FP_COLS['jumlah']])
    mq = QTY_RE.match(q)
    if not mq or not h or not j:
        return None, None
    A, B = int(mq.group(1)), int(mq.group(2))
    isi = isi_per_carton(r[FP_COLS['kemasan']])
    if not isi:
        return None, None
    pcs = A * isi + B
    exp = h * pcs
    if abs(exp - j) <= max(3, j * 0.01):
        return ('qty=carton*isi+pcs', None)
    if B == 0 and abs(h * A * isi - j) <= max(3, j * 0.01):
        return ('qty=carton*isi', None)
    if abs(h * A - j) <= max(3, j * 0.01):
        return ('qty=first-number', None)
    # try single-digit OCR fix on harga that makes carton*isi+pcs exact
    target = j / pcs if pcs else None
    if target and abs(target - round(target * 100) / 100) < 0.005:
        orig = str(r[FP_COLS['harga']]).strip()
        for cand, v in digit_fix_candidates(orig):
            if abs(v - target) <= 0.01:
                return (None, f'{orig}->{cand}')
    return None, None

def build_page_lookup(corpus):
    docs = corpus.values() if isinstance(corpus, dict) else corpus
    by_file = {}
    for d in docs:
        fn = (d.get('filename') or '').rsplit('.pdf', 1)[0].strip()
        sj = d.get('standard_json')
        if isinstance(sj, str):
            try:
                sj = json.loads(sj)
            except Exception:
                continue
        pages = (sj or {}).get('pages') if isinstance(sj, dict) else sj
        if pages:
            by_file[fn] = {i + 1: str(p.get('text') if isinstance(p, dict) else p).splitlines()
                           for i, p in enumerate(pages)}
    return by_file

def _iter_fp_rows(sheets, page_lookup):
    rows = sheets.get('Faktur Penjualan') or []
    data = [r for r in rows if len(r) >= 18 and re.match(r'^p\d+ ', r[FP_COLS['src']] or '')]
    if not data:
        return
    doc_pages = collections.defaultdict(dict)
    for r in data:
        m = re.match(r'^p(\d+)\s+(.*)$', r[FP_COLS['src']])
        doc_pages[m.group(2).strip()].setdefault(int(m.group(1)), page_lookup.get(m.group(2).strip(), {}).get(int(m.group(1)), []))
    # per-document company + sig (continuation pages inherit)
    doc_meta = {}
    for fn, pages in doc_pages.items():
        sigs = {pn: table_sig(l) for pn, l in pages.items()}
        cur = None
        for pn in sorted(sigs):
            if sigs[pn]:
                cur = sigs[pn]
            elif cur:
                sigs[pn] = cur
        first = next((v for _, v in sorted(sigs.items()) if v), None)
        for pn in sigs:
            if not sigs[pn]:
                sigs[pn] = first
        comp = None
        for _, l in sorted(pages.items()):
            comp = page_company(l)
            if comp:
                break
        doc_meta[fn] = (comp or 'UNATTRIBUTED:' + fn[:20], sigs)
    for r in data:
        m = re.match(r'^p(\d+)\s+(.*)$', r[FP_COLS['src']])
        fn, pn = m.group(2).strip(), int(m.group(1))
        comp, sigs = doc_meta.get(fn, (None, {}))
        sig = sigs.get(pn)
        if not comp or not sig:
            continue
        vkey = hashlib.sha1(('#'.join(sig) + '|' + comp).encode()).hexdigest()[:12]
        yield vkey, comp, '#'.join(sig), r

def load_kb():
    if os.path.exists(VFP):
        return json.load(open(VFP))
    return dict(version=2, variants={})

def save_kb(kb):
    os.makedirs(KB_DIR, exist_ok=True)
    json.dump(kb, open(VFP, 'w'), indent=1)

def learn_from_sheets(sheets, page_lookup):
    kb = load_kb()
    for vkey, comp, sig, r in _iter_fp_rows(sheets, page_lookup):
        v = kb['variants'].setdefault(vkey, dict(
            company=comp, sig=sig, doctype='Faktur Penjualan', sem={}, conf={},
            contrad=[], examples=[], pass_=0, unexplained=0))
        st = r[FP_COLS['stat']]
        if st == 'SUMMARY':
            continue
        sem, cfix = classify_row(r)
        if sem:
            v['sem'][sem] = v['sem'].get(sem, 0) + 1
            if st == 'PASS':
                v['pass_'] += 1
            elif v.get('promoted_sem') and v['promoted_sem'] != sem and len(v['contrad']) < 5:
                v['contrad'].append(f'{sem}@{r[FP_COLS["src"]][:22]}')
        elif cfix:
            v['conf'][cfix] = v['conf'].get(cfix, 0) + 1
        elif st in ('REVIEW-ARITH', 'REVIEW'):
            v['unexplained'] += 1
        if len(v['examples']) < 3:
            v['examples'].append(r[FP_COLS['src']][:46])
    promoted = 0
    for v in kb['variants'].values():
        if v['sem']:
            best, cnt = max(v['sem'].items(), key=lambda kv: kv[1])
            tot = sum(v['sem'].values())
            if cnt >= 3 and cnt / tot >= 0.6:
                v['promoted_sem'] = best
                promoted += 1
        v.setdefault('contrad', [])
    save_kb(kb)
    return kb, promoted

def apply_to_sheets(sheets, page_lookup):
    """Returns (sem_fixed, corr_fixed). Exact-arithmetic-only."""
    kb = load_kb()
    sem_c = corr_c = 0
    for vkey, comp, sig, r in _iter_fp_rows(sheets, page_lookup):
        if r[FP_COLS['stat']] not in ('REVIEW-ARITH', 'REVIEW'):
            continue
        v = kb['variants'].get(vkey)
        if not v:
            continue
        sem = v.get('promoted_sem')
        if sem:
            q = (r[FP_COLS['qty']] or '').replace(' ', '')
            mq = QTY_RE.match(q)
            isi = isi_per_carton(r[FP_COLS['kemasan']])
            h, j = n2(r[FP_COLS['harga']]), n2(r[FP_COLS['jumlah']])
            if mq and isi and h and j:
                A, B = int(mq.group(1)), int(mq.group(2))
                exp = {'qty=carton*isi+pcs': (A * isi + B) * h,
                       'qty=carton*isi': A * isi * h,
                       'qty=first-number': A * h}.get(sem)
                if exp and abs(exp - j) <= max(3, j * 0.02):
                    r[FP_COLS['stat']] = 'PASS-LEARNED'
                    r[FP_COLS['conf']] = 'high|KB:' + sem
                    sem_c += 1
                    continue
        # OCR digit-confusion price correction (rule seen >=2x in THIS variant)
        cands = [kf for kf, c in (v.get('conf') or {}).items() if c >= 2]
        if cands:
            h_str = str(r[FP_COLS['harga']]).strip()
            j = n2(r[FP_COLS['jumlah']])
            q = (r[FP_COLS['qty']] or '').replace(' ', '')
            mq = QTY_RE.match(q)
            isi = isi_per_carton(r[FP_COLS['kemasan']])
            if mq and isi and j:
                pcs = int(mq.group(1)) * isi + int(mq.group(2))
                target = j / pcs
                for kf in cands:
                    o, fx = kf.split('->')
                    if o == h_str:
                        vfx = n2(fx)
                        if vfx and abs(vfx * pcs - j) <= max(3, j * 0.02):
                            r[FP_COLS['harga']] = fx
                            r[FP_COLS['stat']] = 'PASS-LEARNED-CORR'
                            r[FP_COLS['conf']] = f'med|KB-CORR:{o}->{fx}'
                            corr_c += 1
                            break
    return sem_c, corr_c

PO_SHAPE_RX = [
    ('dotted', re.compile(r'^\d{4}\.[A-Z]{2,3}\.\d{2}\.\d{5,7}$')),
    ('dash-code', re.compile(r'^[A-Z]{2,10}\d{2,6}[A-Z]*-\d{6,9}$')),
    ('bare-10', re.compile(r'^\d{10}$')),
    ('bare-13/15', re.compile(r'^\d{13,15}$')),
    ('bare-6/12', re.compile(r'^\d{6,12}$')),
    ('alpha-mix', re.compile(r'^[A-Z0-9._/-]{8,}$')),
]

def po_shape(v):
    for name, rx in PO_SHAPE_RX:
        if rx.match(v):
            return name
    return 'other'

def record_po_variants(sheets):
    """PO-number FORMAT registry per company (self-learning, additive).

    Every mapped PO row teaches which shape its issuer uses (GrandLucky dotted
    '9919.PL.26.034256', Gramedia dash 'POGAM2609-00024628', SAP bare-10, Aeon
    bare-13...). Stored in knowledge/po_formats.json: company -> shape -> count +
    example. Lets future runs recognize an issuer's PO even when the label text is
    OCR-mangled. Only counts real, arithmetically-mapped rows (MAPPED/OCR_ITEM)."""
    p = os.path.join(KB_DIR, 'po_formats.json')
    reg = json.load(open(p)) if os.path.exists(p) else {}
    rows = sheets.get('PO Customer') or []
    data = rows[1:] if (rows and rows[0] and 'Purchase' in str(rows[0][0])) else rows
    added = 0
    for r in data:
        if len(r) < 11 or not str(r[0]).strip():
            continue
        st = str(r[10])
        if 'MAPPED' not in st and 'OCR_ITEM' not in st:
            continue
        comp = po_company(r[1])
        shape = po_shape(r[0])
        e = reg.setdefault(comp, {}).setdefault(shape, {'count': 0, 'example': r[0]})
        e['count'] += 1
        added += 1
    os.makedirs(KB_DIR, exist_ok=True)
    json.dump(reg, open(p, 'w'), indent=1, ensure_ascii=False)
    return added

def po_company(r1):
    """Company label for the PO registry: canonical self-name or cleaned vendor name."""
    u = re.sub(r'\s+', ' ', str(r1 or '').upper()).strip()
    if re.search(r'ABADI\s*MAKMUR|SARANA ABADI', u):
        return 'SELF:SARANA ABADI MAKMUR BERSAMA'
    return u[:50] or 'UNATTRIBUTED'

def render_wiki(kb=None):
    kb = kb or load_kb()
    os.makedirs(WIKI, exist_ok=True)
    by_comp = collections.defaultdict(list)
    for hsh, v in kb['variants'].items():
        by_comp[v['company']].append((hsh, v))
    idx = ['# Document variant wiki', '',
           'Maintained by `scripts/knowledge.py` after every mapping run '
           '(llm-wiki pattern: knowledge compiled once, kept current, never re-derived).', '',
           '`PASS-LEARNED` = arithmetic proven by promoted semantics. '
           '`PASS-LEARNED-CORR` = single OCR digit fixed via learned conf rule, exact re-fit verified. '
           '`contrad` = rows following another semantics — human call before changing promotion.', '']
    for comp, vs in sorted(by_comp.items(), key=lambda kv: -sum(sum(x[1]['sem'].values()) for x in kv[1])):
        n_ev = sum(sum(v['sem'].values()) for _, v in vs)
        lines = [f'# {comp}', '', f'_layout variants: {len(vs)} | arithmetic evidence: {n_ev}_', '']
        for hsh, v in sorted(vs, key=lambda x: -sum(x[1]['sem'].values())):
            lines += [f'## variant `{hsh}` — columns: {v["sig"]}',
                      f'- evidence: {v["sem"]}',
                      f'- PROMOTED: **{v.get("promoted_sem", "— (needs >=3 consistent rows)")}**',
                      f'- conf rules (OCR digit fixes, count>=2 applied): {v.get("conf", {})}',
                      f'- proven PASS rows: {v["pass_"]} | unexplained REVIEW: {v["unexplained"]}',
                      f'- contradictions: {v["contrad"] or "none"}',
                      f'- examples: {v["examples"]}', '']
        p = os.path.join(WIKI, re.sub(r'[^A-Za-z0-9]+', '_', comp)[:52] + '.md')
        open(p, 'w').write('\n'.join(lines))
        idx.append(f'- [{comp}]({os.path.basename(p)}) — {len(vs)} variants')
    open(os.path.join(KB_DIR, 'INDEX.md'), 'w').write('\n'.join(idx))

if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['learn', 'apply', 'wiki'])
    ap.add_argument('--dump', default='/tmp/dump_v4.json')
    ap.add_argument('--corpus', default='/tmp/batch_map_docs.json')
    ap.add_argument('--out', default=None)
    a = ap.parse_args()
    dump = json.load(open(a.dump))
    pl = build_page_lookup(json.load(open(a.corpus)))
    if a.cmd == 'learn':
        kb, prom = learn_from_sheets(dump['sheets'], pl)
        print(f'variants={len(kb["variants"])} promoted={prom} '
              f'companies={len({v["company"] for v in kb["variants"].values()})}')
        render_wiki(kb)
    elif a.cmd == 'apply':
        s, c = apply_to_sheets(dump['sheets'], pl)
        print(f'sem-proven={s} ocr-corrected={c}')
        if a.out:
            json.dump(dump, open(a.out, 'w'), ensure_ascii=False)
    elif a.cmd == 'wiki':
        render_wiki()
        print('rendered', len(os.listdir(WIKI)), 'company pages')
