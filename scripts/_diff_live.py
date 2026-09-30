import json, collections

live = json.load(open('/tmp/sheet_now.json'))
mine = json.load(open('/tmp/dump_restore.json'))['sheets']
review_mine = json.load(open('/tmp/dump_restore.json')).get('review') or []

def norm(row):
    return [str(c).strip() for c in row]

for t in ['PO Customer', 'Tanda Terima', 'Surat Jalan', 'Faktur Penjualan']:
    L = [norm(r) for r in live[t][1:]]
    M = [norm(r) for r in mine[t]]
    # align by Source Page col
    spidx = {'PO Customer': 10, 'Tanda Terima': 8, 'Surat Jalan': 3, 'Faktur Penjualan': 15}[t]
    def key(r): return (r[spidx] if len(r) > spidx else '', r[0], r[1], r[2] if len(r) > 2 else '')
    LC = collections.Counter(key(r) for r in L)
    MC = collections.Counter(key(r) for r in M)
    only_live = LC - MC
    only_mine = MC - LC
    diffcells = 0
    lm = {}
    for r in M:
        lm.setdefault(key(r), []).append(r)
    for r in L:
        k = key(r)
        if lm.get(k):
            m = lm[k].pop(0)
            if m != r:
                diffcells += 1
                if diffcells <= 3:
                    print(f'  [{t}] DIFF live!=mine:')
                    print('    live:', [c[:20] for c in r])
                    print('    mine:', [c[:20] for c in m])
    print(f'{t:16} live={len(L)} mine={len(M)} | only-in-live={sum(only_live.values())} only-in-mine={sum(only_mine.values())} cell-diffs={diffcells}')
