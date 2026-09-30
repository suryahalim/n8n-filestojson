import json, collections

live = json.load(open('/tmp/sheet_now.json'))
mine = json.load(open('/tmp/dump_restore.json'))['sheets']

def norm(row): return [str(c).strip() for c in row]
L = [norm(r) for r in live['Faktur Pajak'][1:]]
M = [norm(r) for r in mine['Faktur Pajak']]
key = lambda r: (r[1], r[0])
lm = collections.defaultdict(list)
for r in M: lm[key(r)].append(r)
shown = 0
diffcols = collections.Counter()
for r in L:
    k = key(r)
    if lm.get(k):
        m = lm[k].pop(0)
        if m != r:
            # pad compare
            n = max(len(m), len(r))
            rr = r + ['']*(n-len(r)); mm = m + ['']*(n-len(m))
            cols = [i for i in range(n) if rr[i] != mm[i]]
            for i in cols: diffcols[i] += 1
            if shown < 4 and cols:
                shown += 1
                for i in cols[:4]:
                    print(f'  col{i}: live={rr[i][:26]!r} mine={mm[i][:26]!r}')
                print('  ---')
print('diff column histogram:', dict(diffcols))
