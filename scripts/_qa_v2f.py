import json, re, collections, sys
sys.path.insert(0, '/home/suryahalim/doc-pipeline/scripts')
from map_sheets import num as FN
d = json.load(open('/tmp/dump_v2g.json'))
po = d['sheets']['PO Customer']
data = [r for r in po if 'Purchase' not in str(r[0])]
def f(r, i): return i < len(r) and str(r[i]).strip() != ''
print('rows:', len(data))
print('empty Product Name:', sum(1 for r in data if not f(r, 5)))
print('empty PO No:', sum(1 for r in data if not f(r, 0)))
print('ITEM-FALLBACK:', sum(1 for r in data if 'ITEM-FALLBACK' in str(r[12])))
print('status1:', collections.Counter(str(r[12]).split('|')[0] for r in data).most_common(6))
bad = 0
for r in data:
    if 'MAPPED' not in str(r[12]): continue
    q, pu, pd, pt = FN(r[6]), FN(r[8]), FN(r[9]) or 0, FN(r[10])
    if None in (q, pu, pt): continue
    if abs(q * pu - pd - pt) > max(0.02 * abs(pt), 5):
        bad += 1
print('arith violations MAPPED:', bad)
samb = sum(1 for r in data if re.search(r'SARANA|SAHANA|MAKMUR BERSAMA', str(r[2]), re.I))
print('SAMB-as-issuer:', samb)
ppn = collections.Counter(str(r[3]) for r in data if f(r, 3))
print('PPN:', ppn.most_common(4))
aeon = [r for r in data if 'DUTA BUAH' in str(r[2]) or 'ALPENLIEBE' in str(r[5])]
print('duta-buah sample rows:', len(aeon))
