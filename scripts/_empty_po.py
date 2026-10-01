import json
d = json.load(open('/tmp/dump_v2f.json'))
data = [r for r in d['sheets']['PO Customer'] if 'Purchase' not in str(r[0])]
for r in data:
    if not str(r[0]).strip():
        print('EMPTYPO:', r[2][:22], '|', r[5][:26], '|', r[6], r[7], '|', r[10], '|', str(r[12])[:30])
import collections
print(collections.Counter(str(r[12]) for r in data if 'DUTA' in str(r[2])).most_common(3))
