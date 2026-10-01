import json
d = json.load(open('/tmp/dump_v2e.json'))
po = d['sheets']['PO Customer']
import subprocess
hits = [r for r in po if 'ITEM-FALLBACK' in json.dumps(r)]
print('ITEM-FALLBACK rows:', len(hits))
aeon = [r for r in po if '7000363700' in json.dumps(r) and str(r[11]).startswith('p6')]
print('p6 7000363700 rows:', len(aeon))
for a in aeon[:4]:
    print(a)
