import json, re, collections, sys
sys.path.insert(0, '/home/suryahalim/doc-pipeline/scripts')
import knowledge as KB

rows = json.load(open('/tmp/sheet_now.json'))
corpus = json.load(open('/tmp/batch_map_docs.json'))

# coverage with truncation-aware matching (Source File stem[:28])
fpj = rows['Faktur Pajak']
srcs = set(str(r[18])[:28] for r in fpj[1:] if len(r) > 18)
ef = set((d.get('filename') or '').rsplit('.pdf', 1)[0].strip()[:28]
         for d in corpus.values() if '2026-09-24T12:00' <= (d.get('created') or '') < '2026-09-24T12:20')
print('e-Faktur window docs:', len(ef), '| missing from sheet SourceFile:', len(ef - srcs))

# TT '0007.RC' rows: which booklet/page?
tt = rows['Tanda Terima']
rc = [r for r in tt[1:] if str(r[1]).startswith('0007.RC') or str(r[1]).startswith('0004.RC')]
src = collections.Counter(r[8].split(' ', 1)[-1][:30] for r in rc)
print('RC-style doc rows:', len(rc), '| sources:', src.most_common(4))
pg = collections.Counter(r[8].split(' ', 1)[0] for r in rc)
print('pages:', pg.most_common(6))
print('sample:', rc[0])

# raw text of one such page to see what the OCR actually says
pl = KB.build_page_lookup(corpus)
m = re.match(r'^p(\d+) (.*)$', rc[0][8])
fn, pn = m.group(2).strip(), int(m.group(1))
lines = pl.get(fn, {}).get(pn, [])
for l in lines[:20]:
    print('  |', l[:95])
