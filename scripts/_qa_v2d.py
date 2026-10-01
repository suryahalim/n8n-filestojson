import json, re, collections
d = json.load(open('/tmp/dump_v2d.json'))
po = d['sheets']['PO Customer']
data = [r for r in po if 'Purchase' not in str(r[0])]
def filled(r, i): return i < len(r) and str(r[i]).strip() not in ('', 'None')
print('rows:', len(data))
print('status:', collections.Counter(str(r[12]).split('|')[0] for r in data).most_common(10))
print('issuer top:', collections.Counter(str(r[2]).upper()[:44] for r in data if filled(r, 2)).most_common(10))
SELF = re.compile(r'SARANA|SAHANA|ABADI MAKMUR|MAJU ABADI|KIRANA', re.I)
print('SELF-as-issuer:', sum(1 for r in data if SELF.search(str(r[2]))))
print('ppn vals:', collections.Counter(str(r[3]) for r in data if filled(r, 3)))
junkpat = re.compile(r'SEND TO|^PT\.? SUDAH| SUDAH$|SHIP TO', re.I)
junk = [str(r[2]) for r in data if junkpat.search(str(r[2]))]
print('junk issuers:', len(junk), collections.Counter(j[:24] for j in junk).most_common(4))
print('unique POs:', len(set(str(r[0]) for r in data if str(r[0]).strip())))
pv = collections.defaultdict(collections.Counter)
for r in data:
    if str(r[0]).strip() and str(r[2]).strip():
        pv[str(r[0])][str(r[2]).upper()[:40]] += 1
print('PO->issuer conflicts:', sum(1 for v in pv.values() if len(v) > 1))
