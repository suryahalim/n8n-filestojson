import json, collections

live = json.load(open('/tmp/sheet_now.json'))
mine = json.load(open('/tmp/dump_restore.json'))['sheets']
rev_mine = json.load(open('/tmp/dump_restore.json')).get('review') or []

def norm(row): return [str(c).strip() for c in row]

# Faktur Pajak: compare by (Source File, Billing Number) key cols 18/1? header: SOR,Billing,...
L = [norm(r) for r in live['Faktur Pajak'][1:]]
M = [norm(r) for r in mine['Faktur Pajak']]
key = lambda r: (r[1], r[-2] if len(r) > 18 else r[18], r[0])
LC = collections.Counter(key(r) for r in L)
MC = collections.Counter(key(r) for r in M)
print('Faktur Pajak live=', len(L), 'mine=', len(M),
      'only-live=', sum((LC - MC).values()), 'only-mine=', sum((MC - LC).values()))
lm = collections.defaultdict(list)
for r in M: lm[key(r)].append(r)
cd = 0
for r in L:
    k = key(r)
    if lm.get(k):
        m = lm[k].pop(0)
        if m != r: cd += 1
print('cell-diffs:', cd)

# Review tab
lr = [norm(r) for r in live['OCR Mapping Review'][1:]]
mr = [norm(r) for r in rev_mine]
print('Review live=', len(lr), 'mine=', len(mr))
