import json, re, collections, sys
sys.path.insert(0, '/home/suryahalim/doc-pipeline/scripts')
from map_sheets import num as FN

corpus = json.load(open('/tmp/batch_map_docs.json'))

def lines(t):
    return [l.strip() for l in t.splitlines() if l.strip()]

MONEY = r'[\d][\d.,]*'

def toks(s):
    return s.replace('.', '').replace(',', '.')  # naive IDR money

# ---------- HYPERMART E-PO ----------
def po_hypermart(t, page):
    if 'E-PO' not in t.upper():
        return []
    po = re.search(r'PO#\s*:\s*([0-9A-Z][0-9A-Z./-]{3,})', t)
    if not po:
        return []
    issuer = next((l for l in lines(t) if re.search(r'\bHYPERMART|\bhypermat\b', l, re.I)), '')
    vc = re.search(r'VENDOR\s*:\s*(\d{6,12})', t)
    pn = re.search(r'(?:PPN|PPn)\s*:?\s*(11\.00|11|%)\b', t)
    ppn = '11%' if pn else ('1.1%' if re.search(r'(?:PPN|PPn)\s*:?\s*1\.10\b', t) else '')
    rows = []
    for l in lines(t):
        m = re.match(r'^(\d{1,5})\s+(\S.*?)\s+(\d{10,14})\s+(\d{6,10})\s+(\d+)\s+(\d+)\s+(' + MONEY + r')\s+(' + MONEY + r')\s+(' + MONEY + r')(?:\s+(' + MONEY + r'))?', l)
        if not m:
            continue
        q, name, upc, sku, ca, cb, price, v1, v2, v3 = m.groups()
        qv = float(q); pr = FN(price)
        cands = [FN(x) for x in (v1, v2, v3) if x]
        cands = [c for c in cands if c is not None]
        if not cands or pr is None:
            continue
        tot = max(cands)
        disc = 0.0
        for c in cands:
            if c != tot and abs(qv * pr - c - tot) / max(tot, 1) < 0.03:
                disc = c
        ok = qv and pr and abs(qv * pr - disc - tot) / max(tot, 1) < 0.03
        rows.append([po.group(1), vc.group(1) if vc else '', issuer, ppn, sku, name.strip(),
                     qv, 'EA', pr, disc, tot, page, 'OCR_ITEM' + ('' if ok else '|VERIFY'),
                     ok])
    return rows

# ---------- LION SUPER INDO ----------
def po_lion(t, page):
    po = re.search(r'(?:PO Number|NO PO)\s*:?\s*([A-Z][A-Z0-9]{5,})', t, re.I)
    if not po:
        return []
    if not re.search(r'LION SUPER INDO', t, re.I):
        return []
    issuer = 'PT. LION SUPER INDO'
    rows = []
    for l in lines(t):
        mm = re.match(r'^\d+\.\s+(\d{6,12})\s+(.+?)\s+(\d+)\s+-\s+(' + MONEY + r')\s+(' + MONEY + r')\s+(' + MONEY + r')\s+(' + MONEY + r')\s*$', l)
        if not mm:
            continue
        code, name, qty, price, disc, ppnv, total = mm.groups()
        q = float(qty); pr = FN(price); dc = FN(disc); tt = FN(total)
        ok = all(x is not None for x in (pr, dc, tt)) and abs(q * pr - dc - tt) / max(tt, 1) < 0.03
        rows.append([po.group(1), '', issuer, '11%' if FN(ppnv) and FN(ppnv) > 5 else '1.1%',
                     code, name.strip(), q, 'EA', pr, dc, tt, page,
                     'OCR_ITEM' + ('' if ok else '|VERIFY'), ok])
    return rows

# ---------- ASTRO (PURCHASE ORDER DETAILS) ----------
def po_astro(t, page):
    if 'PURCHASE ORDER DETAILS' not in t.upper():
        return []
    po = re.search(r'PO Number\s*\n?\s*([A-Z0-9/]+)', t)
    iss = re.search(r'(?:SHIP TO|BILL TO)\s*\n\s*(PT[^\n]+)', t, re.I)
    issuer = iss.group(1).strip() if iss else ''
    vc = re.search(r'Vendor\s*\n\s*(\d{4,8})', t, re.I)
    rows = []
    for l in lines(t):
        m = re.match(r'^([A-Z0-9-]{6,24})\s+(.+?)\s+(\d+)\s+(' + MONEY + r')\s+(' + MONEY + r')$', l)
        if not m or m.group(1) in (po.group(1) if po else '?',):
            continue
        sku, name, q, price, tot = m.groups()
        q = float(q); pr = FN(price); tt = FN(tot)
        ok = pr and tt and abs(q * pr - tt) / max(tt, 1) < 0.03
        rows.append([po.group(1) if po else '', vc.group(1) if vc else '', issuer, '',
                     sku, name.strip(), q, 'EA', pr, 0.0, tt, page,
                     'OCR_ITEM' + ('' if ok else '|VERIFY'), ok])
    return rows

cnt = collections.Counter(); ar = collections.Counter(); ex = {}
for did, d in corpus.items():
    for p in (d.get('standard_json') or {}).get('pages', []):
        t = p.get('text') or ''
        for name, fn_ in (('hypermart', po_hypermart), ('lion', po_lion), ('astro', po_astro)):
            r = fn_(t, p['page'])
            for row in r:
                cnt[name] += 1
                ar[name + ('-PASS' if row.pop() else '-FAIL')] += 1
                if not ex.get(name):
                    ex[name] = str(row)[:250]
print('rows:', dict(cnt))
print('arith:', dict(ar))
for k, v in ex.items():
    print(k, 'SAMPLE:', v)
