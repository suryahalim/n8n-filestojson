import json, sys, re
sys.path.insert(0, '/home/suryahalim/doc-pipeline/scripts')
import knowledge as KB
corpus = json.load(open('/tmp/batch_map_docs.json'))
lp = KB.build_page_lookup(corpus)
pages = lp['7000362345 - 7000362399']
hits = []
for p in sorted(pages):
    txt = '\n'.join(pages[p])
    if re.search(r'PURCHASE ORDER|PO Issued|Nomor PO|ORDER NUMBER', txt, re.I):
        hits.append(p)
print('PO pages (1..166):', hits)
for p in hits[:6]:
    txt = '\n'.join(pages[p])
    print('='*20, 'PAGE', p, '='*20)
    print(txt[:1400])
