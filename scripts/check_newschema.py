import json, re, sys
sys.path.insert(0, '/home/suryahalim/doc-pipeline/scripts')
import knowledge as KB
rows = json.load(open('/tmp/sheet_now.json'))
d = rows['PO Customer'][1:]
odd = [x for x in d if not re.match(r'^\d{1,3}(\.\d{3})*,\d\d$|^\d+([.,]\d+)?$', str(x[9]) or '')]
for x in odd[:6]:
    print('ODD TOTAL:', [str(c)[:28] for c in x[3:10]], '|', x[11][:22])
print()
noq = [x for x in d if not str(x[5]).strip()]
for x in noq:
    print('EMPTY QTY:', [str(c)[:26] for c in x[3:10]], '|', x[10][:26], '|', x[11][:16])
# one odd row vs raw page
corpus = json.load(open('/tmp/batch_map_docs.json'))
pl = KB.build_page_lookup(corpus)
target = [r for r in d if str(r[9]).strip() == '12']
if target:
    x = target[0]
    m = re.match(r'^p(\d+) (.*)', x[10])
    lines = pl.get(x[10].split(' ', 1)[1], {}).get(int(m.group(1)), [])
    for l in lines:
        if x[3] in l or 'PILIHAN' in l.upper():
            print('RAW:', l[:120])
