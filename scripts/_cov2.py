import json, re, collections, sys
sh = json.load(open('/tmp/sheet_now.json'))
IN = {'Faktur Penjualan': 15, 'PO Customer': 10, 'Tanda Terima': 8, 'Surat Jalan': 8,
      'Dokumen Pelunasan': 9, 'OCR Mapping Review': 4, 'Faktur Pajak': 11}
landed = collections.defaultdict(set)
for t, i in IN.items():
    if t not in sh:
        continue
    for r in sh[t][1:]:
        if len(r) > i:
            m = re.match(r'p(\d+)\s+7000363300', str(r[i]))
            if m:
                landed[t].add(int(m.group(1)))
for t, s in landed.items():
    print(t, len(s))
corpus = json.load(open('/tmp/batch_map_docs.json'))
d = corpus['f44358d37d0f4be79f92318e218ea5ac']
pgs = {p['page']: (p.get('text') or '') for p in (d.get('standard_json') or {}).get('pages', [])}
all_l = set().union(*landed.values()) if landed else set()
po_miss = [p for p in sorted(pgs) if 'PURCHASE ORDER' in pgs[p].upper() and p not in all_l]
print('doc pages:', len(pgs), '| PO-marker pages not landed:', len(po_miss), po_miss[:20])
