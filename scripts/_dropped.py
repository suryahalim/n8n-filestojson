import json
def names(p):
    d = json.load(open(p))
    return {(str(r[0]), str(r[5])) for r in d['sheets']['PO Customer'] if 'Purchase' not in str(r[0]) and str(r[5]).strip()}
a = names('/tmp/dump_v2f.json')
b = names('/tmp/dump_v2g.json')
gone = sorted(a - b)
print('dropped by junk filter:', len(gone))
import collections
pat = collections.Counter(g[1][:14] for g in gone)
for k, v in pat.most_common(15):
    print(v, repr(k))
for g in gone[:8]:
    print('  x', g[0][:14], '|', g[1][:44])
