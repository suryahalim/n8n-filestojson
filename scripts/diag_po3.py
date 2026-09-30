import json, re, sys, collections
sys.path.insert(0, '/home/suryahalim/doc-pipeline/scripts')
import knowledge as KB

corpus = json.load(open('/tmp/batch_map_docs.json'))
pl = KB.build_page_lookup(corpus)
rows = json.load(open('/tmp/sheet_now.json'))
po = rows['PO Customer']
empty = [x for x in po[1:] if not (x[0] or '').strip()]

# Ordered PO-label patterns (tight, verified against real pages)
PATS = [
    (re.compile(r'PO[#\s]*[:=]\s*([A-Z0-9][A-Z0-9./_-]{5,})', re.I), 'PO#'),
    (re.compile(r'Purchase Order No[.\s]*[:=]?\s+([A-Z0-9][A-Z0-9./_-]{5,})', re.I), 'Purchase Order No.'),
    (re.compile(r'PURCHASE\s+ORDER\s*(?:\([A-Z]+\))?\s*\n+\s*(?:No\.?\s*[:=]?\s*)?(\d{6,12})', re.I), 'PURCHASE ORDER+bare'),
    (re.compile(r'[:=]\s*(\d{10})\b(?=[^\n]*Purch\.?\s*Grp)', re.I), 'colon SAP10'),
    (re.compile(r'^\s*(\d{10})\s*$', re.M), 'SAP10 bare'),
    (re.compile(r'NO[.\s]*PO\s*[:=]\s*([A-Z0-9][A-Z0-9./_-]{5,})', re.I), 'NO PO:'),
    (re.compile(r'Nomor\s*PO\s*[:=]\s*([A-Z0-9][A-Z0-9./_-]{5,})', re.I), 'Nomor PO:'),
    (re.compile(r'PO\s*No\.?\s*[:=]\s*([A-Z0-9][A-Z0-9./_-]{5,})', re.I), 'PO No:'),
    (re.compile(r'PO\s*NUMBER\s*[:=]?\s*([A-Z0-9][A-Z0-9./_-]{5,})', re.I), 'PO NUMBER'),
    (re.compile(r'ORDER\s*(?:NO|NUMBER)\s*[:=]\s*([A-Z0-9][A-Z0-9./_-]{6,})', re.I), 'ORDER NO:'),
    (re.compile(r'PR\s*No\s*[:=\n]\s*(\d{6,})', re.I), 'PR No:'),
    (re.compile(r'(?<![\d-])\b([A-Z]{2,10}\d{2,6}[A-Z]*-\d{6,9})\b'), 'dash-code'),
    (re.compile(r'(?<![\dA-Z.])\b(\d{4}\.[A-Z]{2,3}\.\d{2}\.\d{5,7})\b'), 'dotted-code'),
]
DATEISH = re.compile(r'^(?:Jan|Feb|Mar|Apr|Mei|Jun|Jul|Agu|Sep|Okt|Nov|Des)|^\d{2}[-/]\d{2}|^20\d\d$', re.I)

def find_po(t):
    # SAP bare number only credible if the page is a purchase-order page
    POISH = re.search(r'PURCHASE\s*ORDER|Purch\.?\s*Grp|P\.O\b', t, re.I)
    for rx, label in PATS:
        if label in ('SAP10 bare', 'colon SAP10') and not POISH:
            continue
        for mm in rx.finditer(t):
            v = mm.group(1)
            if DATEISH.search(v):
                continue
            return label, v[:28]
    return None

pages = {}
for x in empty:
    m = re.match(r'^p(\d+) (.*)$', x[9])
    pages.setdefault((m.group(2).strip(), int(m.group(1))), 0)
    pages[(m.group(2).strip(), int(m.group(1)))] += 1

res = collections.Counter()
none = []
for (fn, pn), c in sorted(pages.items()):
    t = '\n'.join(pl.get(fn, {}).get(pn, []))
    hit = find_po(t)
    if hit:
        res[hit[0]] += c
    else:
        res['NONE'] += c
        none.append((fn, pn, c))
print('rows resolvable on own page:', sum(v for k, v in res.items() if k != 'NONE'), '/', len(empty))
for k, v in res.most_common(): print(f'  {k}: {v}')
print()
for fn, pn, c in none[:20]:
    print('NONE:', fn[:30], f'p{pn}', f'({c} rows)')
