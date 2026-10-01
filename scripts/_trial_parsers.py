import json, re, collections, sys
sys.path.insert(0, '/home/suryahalim/doc-pipeline/scripts')
from map_sheets import num as fnum

corpus = json.load(open('/tmp/batch_map_docs.json'))

def lines(t):
    return [l.strip() for l in t.splitlines() if l.strip()]

# ---------- HYPERMART E-PO ----------
def po_hypermart(t, page):
    po = re.search(r'PO#\s*:\s*([0-9A-Z][0-9A-Z./-]{3,})', t)
    if not po:
        return []
    issuer = ''
    for l in lines(t):
        if re.search(r'(HYPERMART|hypermat)', l, re.I):
            issuer = l.strip()
            break
    vendor = re.search(r'VENDOR\s*:\s*(\d{6,12})', t)
    ppm = re.search(r'PPN\s*:?\s*([0-9.,]+)', t)
    ppn = ''
    if ppm:
        try:
            ppn = float(ppm.group(1).replace(',', '.'))
            ppn = '11%' if 8 <= ppn <= 15 else '1.1%'
        except ValueError:
            ppn = ''
    rows = []
    for l in lines(t):
        m = re.match(r'^(\d{1,5})\s+(\S.*?)\s+(\d{10,14})\s+(\d{6,10})\s+(\d+)\s+(\d+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)', l)
        if not m:
            continue
        qty, name, upc, sku, case, g2, price, tot, d1, d2 = m.groups()
        q = float(qty); pr = float(price.replace('.', '').replace(',', '.'))
        tt = float(tot.replace('.', '').replace(',', '.'))
        ok = abs(q * pr - tt) / max(tt, 1) < 0.03
        rows.append([po.group(1), vendor.group(1) if vendor else '', ppn, sku, name.strip(),
                     q, 'EA', pr, '', tt, page, 'OCR_ITEM', ok])
    return rows

# ---------- LION SUPER INDO (PONUMBER + NOPO + TGL PO) ----------
def po_lion(t, page):
    po = re.search(r'PO Number\s*:?\s*([A-Z0-9]{6,})', t) or re.search(r'NO PO\s*:\s*([A-Z0-9]{6,})', t)
    if not po:
        return []
    issuer = ''
    m = re.search(r'(?:PT\.?\s*LION SUPER INDO|SUPER INDO)', t, re.I)
    if m:
        issuer = 'PT. LION SUPER INDO'
    else:
        for l in lines(t)[:8]:
            if l.upper().startswith('PT'):
                issuer = l
                break
    rows = []
    for l in lines(t):
        mm = re.match(r'^\s*\d+\.\s+(\d{6,12})\s+(.+?)\s+(\d+)\s+-\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s*$', l)
        if not mm:
            continue
        code, name, qty, price, disc, ppn, total = mm.groups()
        q = float(qty)
        f = lambda v: float(v.replace('.', '').replace(',', '.'))
        pr, dt, tot = f(price), f(disc), f(total)
        ok = abs(q * pr - dt) / max(tot, 1) < 0.03
        rows.append([po.group(1), '', '11%', code, name.strip(), q, 'EA', pr, '', tot, page, 'OCR_ITEM', ok])
    return rows

cnt = collections.Counter(); arith = collections.Counter(); ex = {}
for did, d in corpus.items():
    for p in (d.get('standard_json') or {}).get('pages', []):
        t = p.get('text') or ''
        if 'E-PO' in t.upper() and 'PURCHASE ORDER' in t.upper():
            r = po_hypermart(t, p['page'])
            for row in r:
                cnt['hypermart'] += 1
                arith['hypermart-' + str(row.pop())] += 1
                if len(ex.get('hypermart', [])) < 1 and r:
                    ex['hypermart'] = [str(x)[:38] for x in r[0]]
        if re.search(r'LION SUPER INDO', t, re.I) and 'PURCHASE ORDER' in t.upper():
            r = po_lion(t, p['page'])
            for row in r:
                cnt['lion'] += 1
                arith['lion-' + str(row.pop())] += 1
                if not ex.get('lion') and r:
                    ex['lion'] = [str(x)[:38] for x in r[0]]
print('extracted rows:', dict(cnt))
print('arith proof:', dict(arith))
for k, v in ex.items():
    print(k, 'sample:', v)
