import json, re, collections, sys
sys.path.insert(0, '/home/suryahalim/doc-pipeline/scripts')
import knowledge as KB

rows = json.load(open('/tmp/sheet_now.json'))
po = rows['PO Customer'][1:]

def nums_in(s):
    return re.findall(r'\d[\d.,%]*', s)

glued = []
for x in po:
    prod = x[3] or ''
    if x[4] or x[6] or x[8]:      # only un-split rows: qty/price/total empty
        continue
    tail = re.sub(r'^\s*\d{6,14}\b', '', prod)   # drop leading code
    tail = re.sub(r'^\s*\(?\d{10,14}\)?\b', '', tail)  # drop barcode after code
    ns = nums_in(tail)
    if len(ns) >= 3 and prod:
        glued.append(x)

print('glued un-split rows:', len(glued), 'of', len(po))
print(collections.Counter(x[10].split('|')[0] for x in glued))
bysrc = collections.defaultdict(list)
for x in glued:
    bysrc[x[9].split(' ', 2)[-1][:26]].append(x)
for k, v in sorted(bysrc.items(), key=lambda kv: -len(kv[1])):
    print(f'\n### {k}: {len(v)} rows | vendor={v[0][1][:34]!r} PO={v[0][0]}')
    for r in v[:3]:
        print('   >', r[3][:105])
        print('     qty/uon/price/disc/total:', [r[4], r[5], r[6], r[7], r[8]])
