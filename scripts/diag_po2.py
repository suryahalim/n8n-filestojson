import json, re, sys, collections
sys.path.insert(0, '/home/suryahalim/doc-pipeline/scripts')
import knowledge as KB

corpus = json.load(open('/tmp/batch_map_docs.json'))
pl = KB.build_page_lookup(corpus)
rows = json.load(open('/tmp/sheet_now.json'))
po = rows['PO Customer']
empty = [x for x in po[1:] if not (x[0] or '').strip()]

# tight PO-label patterns, checked in order; must look like a PO code not a date
PATS = [
    (r'PO[#\s]*[:=]\s*([A-Z0-9][A-Z0-9./_-]{5,})', 'PO#:'),
    (r'Purchase Order No[.\s]*[:=]?\s+([A-Z0-9][A-Z0-9./_-]{5,})', 'Purchase Order No.'),
    (r'NO[.\s]*PO\s*[:=]\s*([A-Z0-9][A-Z0-9./_-]{5,})', 'NO PO:'),
    (r'Nomor\s*PO\s*[:=]\s*([A-Z0-9][A-Z0-9./_-]{5,})', 'Nomor PO:'),
    (r'PO\s*No\.?\s*[:=]\s*([A-Z0-9][A-Z0-9./_-]{5,})', 'PO No:'),
    (r'PO\s*NUMBER\s*[:=]?\s*([A-Z0-9][A-Z0-9./_-]{5,})', 'PO NUMBER'),
    (r'ORDER\s*(?:NO|NUMBER)\s*[:=]\s*([A-Z0-9][A-Z0-9./_-]{5,})', 'ORDER NO:'),
    (r'PR\s*No\s*[:=\n]\s*(\d{6,})', 'PR No:'),
    (r'(?<![\d-])\b([A-Z]{2,10}\d{2,6}[A-Z]*-\d{6,9})\b', 'dash-code'),
    (r'(?<![\dA-Z])\b(\d{4}\.[A-Z]{2,3}\.\d{2}\.\d{5,7})\b', 'dotted-code'),
]
DATEISH = re.compile(r'^(Jan|Feb|Mar|Apr|Mei|Jun|Jul|Agu|Sep|Okt|Nov|Des|\d{2}[-/]\d{2}|\d{4}$)', re.I)

def find_po(t):
    for pat, label in PATS:
        for mm in re.finditer(pat, t, re.I):
            v = mm.group(1)
            if re.search(r'\d', v) and not DATEISH.search(v) and not v.endswith('-2026'):
                return label, v[:28]
    return None

# per empty source page, classify
src_pages = {}
for x in empty:
    m = re.match(r'^p(\d+) (.*)$', x[9])
    fn, pn = m.group(2).strip(), int(m.group(1))
    src_pages.setdefault((fn, pn), 0)
    src_pages[(fn, pn)] += 1

cat = collections.Counter()
per_doc = collections.defaultdict(collections.Counter)
none_pages = []
for (fn, pn), c in sorted(src_pages.items()):
    t = '\n'.join(pl.get(fn, {}).get(pn, []))
    hit = find_po(t)
    if hit:
        cat[hit[0]] += c
        per_doc[fn[:24]][hit[0]] += c
    else:
        # try previous page of same doc with same vendor
        cat['NONE'] += c
        none_pages.append((fn, pn, c))

print('rows resolvable by label on OWN page:', sum(v for k, v in cat.items() if k != 'NONE'), '| still none:', cat['NONE'])
print()
for k, v in per_doc.items():
    print(f'  {k:26}', dict(v))
print()
print('NONE pages:', len(none_pages))
for fn, pn, c in none_pages[:12]:
    print(f'  {fn[:26]} p{pn} ({c} rows)')
