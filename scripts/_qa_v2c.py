import json, re, collections

d = json.load(open('/tmp/dump_v2c.json'))
po = d['sheets']['PO Customer']
data = [r for r in po if 'Purchase' not in str(r[0])]
def filled(r, i): return i < len(r) and str(r[i]).strip() not in ('', 'None')
print('rows:', len(data))
print('fill:', {k: sum(1 for r in data if filled(r, i)) for k, i in
      {'PO': 0, 'VCode': 1, 'Issuer': 2, 'PPN': 3, 'Code': 4, 'Name': 5, 'Qty': 6, 'Pr': 8, 'Tot': 10}.items()})
print('status:', collections.Counter(str(r[12]).split('|')[0] for r in data).most_common(10))
print('issuer top:', collections.Counter(str(r[2]).upper()[:44] for r in data if filled(r, 2)).most_common(12))
SELF = re.compile(r'SARANA|SAHANA|ABADI MAKMUR|MAJU ABADI|KIRANA', re.I)
print('SELF-as-issuer:', sum(1 for r in data if SELF.search(str(r[2]))))
print('ppn vals:', collections.Counter(str(r[3]) for r in data if filled(r, 3)))
print('PO empty rows:', sum(1 for r in data if not str(r[0]).strip()))
print('unique POs:', len(set(str(r[0]) for r in data if str(r[0]).strip())))
# one PO = one issuer
pv = collections.defaultdict(collections.Counter)
for r in data:
    if str(r[0]).strip() and str(r[2]).strip():
        pv[str(r[0])][str(r[2]).upper()[:40]] += 1
conf = {k: v for k, v in pv.items() if len(v) > 1}
print('PO->issuer conflicts:', len(conf))
# arith violation among MAPPED
def n2(s):
    s = str(s).strip()
    if not s: return None
    try:
        if re.fullmatch(r'\d{1,3}(\.\d{3})+(,\d+)?', s): return float(s.replace('.', '').replace(',', '.'))
        if re.fullmatch(r'\d{1,3}(,\d{3})+(\.\d+)?', s): return float(s.replace(',', ''))
        return float(s.replace(',', '.'))
    except Exception: return None
bad = 0
for r in data:
    if str(r[12]).split('|')[0] != 'MAPPED': continue
    q, p, dd, t = (n2(r[i]) if filled(r, i) else None for i in (6, 8, 9, 10))
    if None in (q, p, t): bad += 1; continue
    net = q * p - (dd or 0)
    if abs(net - t) > max(1.0, t * 0.001):
        f = 1.11 if str(r[3]).strip() == '11%' else (1.011 if str(r[3]).strip() == '1.1%' else None)
        if not (f and abs(net * f - t) <= max(1.0, t * 0.01)):
            bad += 1
print('MAPPED arith violations:', bad)
# non-PT issuer check
nonpt = [str(r[2]) for r in data if filled(r, 2) and not str(r[2]).upper().startswith(('PT', 'CV'))]
print('issuer non-PT:', len(nonpt), collections.Counter(x[:28] for x in nonpt).most_common(6))
