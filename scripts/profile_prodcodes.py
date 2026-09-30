import json, re, collections

rows = json.load(open('/tmp/sheet_now.json'))
po = rows['PO Customer'][1:]

def classify(v):
    v = v.strip()
    if re.match(r'^\d{9,14}\s+\D', v):           return 'A: lead 9-14 code + desc'
    if re.match(r'^\d{6,8}\s+\D', v):            return 'B: lead 6-8 code + desc'
    if re.match(r'^\d{6,8}\s+\(?\d{10,14}\)?\s+\D', v): return 'C: code + barcode + desc'
    if re.match(r'^\d{6,8}\s', v) and re.search(r'\d{5,}.*\d', v[8:]): return 'D: code + WHOLE LINE glued'
    if re.match(r'^[A-Z]', v) and not re.search(r'\d{6,}', v): return 'E: desc only, NO code'
    return 'F: other'

by = collections.defaultdict(list)
for x in po:
    if x[3]:
        by[classify(x[3])].append(x)

for k in sorted(by):
    print(f'== {k} : {len(by[k])} rows ==')
    srcs = collections.Counter(r[9].split(" ", 2)[-1][:24] for r in by[k])
    print('   sources:', dict(srcs.most_common(4)))
    ex = by[k][0]
    print(f'   qty={ex[4]!r} uon={ex[5]!r} price={ex[6]!r} disc={ex[7]!r} total={ex[8]!r}')
    for r in by[k][:3]:
        print('   >', r[3][:92])
    print()
