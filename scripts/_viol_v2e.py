import json, re
d = json.load(open('/tmp/dump_v2e.json'))
po = [r for r in d['sheets']['PO Customer'] if 'Purchase' not in str(r[0])]
def n2(x):
    s = str(x).strip()
    if not s: return None
    try: return float(s.replace(',', '.') if re.fullmatch(r'-?[\d]*\d[.,]\d{1,2}', s) else s.replace(',', '').replace('.', '')) if re.fullmatch(r'-?[\d.,]+', s) else float(s)
    except Exception: return None
bad = []
for r in po:
    if 'MAPPED' not in str(r[12]): continue
    q, pu, pd, pt = n2(r[6]), n2(r[8]), n2(r[9]) or 0, n2(r[10])
    if None in (q, pu, pt): continue
    if abs(q * pu - pd - pt) > max(0.02 * abs(pt), 5):
        bad.append(r)
print('violations:', len(bad))
import collections
print(collections.Counter(str(r[12]) for r in bad).most_common(8))
for r in bad[:5]:
    print(r[0][:16], '|', r[5][:20], '| q', r[6], 'p', r[8], 'd', r[9], 't', r[10], '|', str(r[12])[:38])
