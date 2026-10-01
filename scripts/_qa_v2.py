import json, re, collections

d = json.load(open('/tmp/dump_v2.json'))
po = d['sheets']['PO Customer']
print('rows:', len(po))
print('header:', d.get('header') or HEAD if False else po[0][:4] if po and 'Purchase' in str(po[0][0]) else 'NO-HEADER-ROW')
data = po[1:] if po and 'Purchase' in str(po[0][0]) else po

def filled(r, i):
    return i < len(r) and str(r[i]).strip() not in ('', 'None')

cols = {'PO': 0, 'VCode': 1, 'Issuer': 2, 'PPN': 3, 'Code': 4, 'Name': 5, 'Qty': 6,
        'UON': 7, 'Price': 8, 'Disc': 9, 'Total': 10, 'SP': 11, 'St': 12}
print('fill rate:', {k: sum(1 for r in data if filled(r, i)) for k, i in cols.items()})
print('status:', collections.Counter(str(r[12]).split('|')[0] for r in data).most_common(8))
print('issuer top:', collections.Counter(str(r[2]).upper()[:40] for r in data if filled(r, 2)).most_common(12))
print('vcode distinct:', len(set(str(r[1]) for r in data if filled(r, 1))),
      '| sample:', [str(r[1]) for r in data if filled(r, 1)][:8])
print('ppn:', collections.Counter(str(r[3]) for r in data if filled(r, 3)))
# self never issuer/vcode-name
SELF = re.compile(r'SARANA ABADI|MAKMUR BERSAMA|SAHANA|KIRANA|MAJU ABADI', re.I)
print('SELF-as-issuer rows:', sum(1 for r in data if SELF.search(str(r[2]))))
# issuer non-PT count
nonpt = [str(r[2]) for r in data if filled(r, 2) and not str(r[2]).upper().lstrip().startswith(('PT', 'CV'))]
print('issuer without PT prefix:', len(nonpt), collections.Counter(x[:30] for x in nonpt).most_common(6))
# arithmetic violation double-check
def n2(s):
    s = str(s).strip()
    if not s:
        return None
    try:
        if re.fullmatch(r'\d{1,3}(\.\d{3})+(,\d+)?', s): return float(s.replace('.', '').replace(',', '.'))
        if re.fullmatch(r'\d{1,3}(,\d{3})+(\.\d+)?', s): return float(s.replace(',', ''))
        return float(s.replace(',', '.'))
    except Exception:
        return None
bad = 0
for r in data:
    q, p, dd, t = (n2(r[i]) if filled(r, i) else None for i in (6, 8, 9, 10))
    if None in (q, p, t) or str(r[12]).split('|')[0] != 'MAPPED':
        continue
    if abs(q * p - (dd or 0) - t) > max(1.0, t * 0.001) and abs((q * p - (dd or 0)) * 1.11 - t) > max(1.0, t * 0.01) and abs((q * p - (dd or 0)) * 1.011 - t) > max(1.0, t * 0.01):
        bad += 1
print('MAPPED with arith violation (double-check):', bad)
