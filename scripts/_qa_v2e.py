import json, re, collections
d = json.load(open('/tmp/dump_v2e.json'))
po = d['sheets']['PO Customer']
data = [r for r in po if 'Purchase' not in str(r[0])]
def f(r, i): return i < len(r) and str(r[i]).strip() != ''
L = len(data)
print('rows:', L)
print('empty PO No:', sum(1 for r in data if not f(r, 0)))
print('empty Product Name:', sum(1 for r in data if not f(r, 5)))
print('  ^ breakdown of no-name rows:', collections.Counter(str(r[12]).split('|')[0] for r in data if not f(r, 5)).most_common(6))
print('PO-HEADER-NOITEMS:', sum(1 for r in data if 'PO-HEADER-NOITEMS' in str(r[12])))
print('ITEM-FALLBACK:', sum(1 for r in data if 'ITEM-FALLBACK' in str(r[12])))
print('still OCR_PAGE_REVIEW:', sum(1 for r in data if 'OCR_PAGE_REVIEW' in str(r[12])))
def n2(x):
    try: return float(str(x).replace('.', '').replace(',', '.') if re.fullmatch(r'[\d.,]+', str(x)) else x)
    except Exception: return None
bad = 0
for r in data:
    if 'MAPPED' not in str(r[12]): continue
    q, pu, pd, pt = n2(r[6]), n2(r[8]), n2(r[9]) or 0, n2(r[10])
    if None in (q, pu, pt): continue
    if abs(q * pu - pd - pt) > max(0.02 * pt, 5) and abs(q * pu - pd - pt) / max(pt, 1) > 0.02:
        bad += 1
print('arith violations (MAPPED):', bad)
samp = [r for r in data if r[10:] and str(r[12]).startswith('MAPPED|ITEM-FALLBACK')][:3]
for s in samp:
    print(' sample:', s[0][:18], '|', s[2][:24], '|', s[4], '|', s[5][:26], '|', s[6], s[7], '|', s[8], '->', s[10])
