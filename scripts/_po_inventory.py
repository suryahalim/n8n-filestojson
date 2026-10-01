import json, re, collections, sys

corpus = json.load(open('/tmp/batch_map_docs.json'))

def layout(t):
    u = t.upper()
    if 'PURCHASE ORDER' not in u and not re.search(r'\bNO PO\b|PO Number|PO#|Nomor PO|P\.O No|ORDER NUMBER', u):
        return None
    if 'PURCHASE ORDER DETAILS' in u:
        return 'ASTRO-details'
    if re.search(r'PO#\s*:', t) and 'E-PO' in u:
        return 'HYPERMART-epo'
    if re.search(r'PO#\s*:', t):
        return 'PODASH-general'
    if re.search(r'PO Number\s*:', t):
        return 'PONUMBER'
    if re.search(r'NO PO\s*:', t):
        return 'NOPO'
    if re.search(r'Nomor PO|Nomor P\.O', t, re.I):
        return 'NOMORPO'
    if re.search(r'P\.O No', t):
        return 'PONO'
    if re.search(r'ORDER NO\b', u):
        return 'ORDERNO'
    return 'OTHER-PO'

cnt = collections.Counter()
samps = {}
for did, d in corpus.items():
    for p in (d.get('standard_json') or {}).get('pages', []):
        t = p.get('text') or ''
        lay = layout(t)
        if lay:
            cnt[lay] += 1
            samps.setdefault((lay, did[:8]), t)
for lay, n in cnt.most_common():
    print(lay, n)
# dump one full sample per layout for parser design
out = open('/tmp/po_layouts.txt', 'w')
seen = set()
for (lay, x), t in samps.items():
    if lay in seen:
        continue
    seen.add(lay)
    out.write('=' * 30 + ' ' + lay + ' ' + '=' * 30 + '\n' + t[:2600] + '\n\n')
out.close()
print('samples dumped:', len(seen))
