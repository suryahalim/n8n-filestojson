import json, re, sys, collections
sys.path.insert(0, '/home/suryahalim/doc-pipeline/scripts')
import knowledge as KB

corpus = json.load(open('/tmp/batch_map_docs.json'))
FID = '7000362345 - 7000362399'
lp = KB.build_page_lookup(corpus)
pages = lp[FID]
print('doc in cached corpus pages:', len(pages))

sheet = json.load(open('/tmp/sheet_now.json'))
print('tabs:', list(sheet.keys()))

# source-page ranges of every doc: find pages present in sheet to learn the scheme
SPIDX = {'Faktur Penjualan': 15, 'PO Customer': 10, 'Tanda Terima': 8,
         'Surat Jalan': 8, 'Faktur Pajak': 11}
allsp = set()
for tab, rows in sheet.items():
    idx = SPIDX.get(tab)
    if idx is None:
        continue
    for r in rows[1:]:
        try:
            allsp.add(int(str(r[idx]).split('.')[0]))
        except (ValueError, IndexError):
            pass
print('distinct source pages in sheet:', len(allsp), '| min', min(allsp), '| max', max(allsp))

# what does the doc's first page text look like vs pages already in sheet
for p in sorted(pages)[:4]:
    print('--- page', p, (pages[p][0][:60] if pages[p] else ''))
