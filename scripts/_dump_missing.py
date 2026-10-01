import json, re
sh = json.load(open('/tmp/sheet_now.json'))
IN = {'Faktur Penjualan': 15, 'PO Customer': 10, 'Tanda Terima': 8, 'Surat Jalan': 8,
      'Dokumen Pelunasan': 9, 'OCR Mapping Review': 4, 'Faktur Pajak': 11}
landed = set()
for t, i in IN.items():
    if t not in sh:
        continue
    for r in sh[t][1:]:
        if len(r) > i:
            m = re.match(r'p(\d+)\s+7000362345', str(r[i]))
            if m:
                landed.add(int(m.group(1)))
corpus = json.load(open('/tmp/batch_map_docs.json'))
d = corpus['058311d6333d4f5cb00d245c6a215ae5']
pgs = {p['page']: (p.get('text') or '') for p in (d.get('standard_json') or {}).get('pages', [])}
miss = [p for p in sorted(pgs) if p not in landed]
out = open('/tmp/missing_pages.txt', 'w')
for p in miss:
    out.write('=' * 24 + ' PAGE ' + str(p) + ' ' + '=' * 24 + '\n' + pgs[p] + '\n')
out.close()
print('landed pages:', len(landed), '| missing pages:', len(miss))
