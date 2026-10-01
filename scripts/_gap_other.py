import json, re, sys, collections
sys.path.insert(0, '/home/suryahalim/doc-pipeline/scripts')
import map_sheets as MS

corpus_all = json.load(open('/tmp/batch_map_docs.json'))
wins = [('2026-09-24T12:00', '2026-09-24T12:20'), ('2026-09-24T13:00', '2026-09-26T23:59')]
corpus = {k: v for k, v in corpus_all.items()
          if any((not s or (v.get('created') or '') >= s) and (not u or (v.get('created') or '') < u)
                 for s, u in wins)}

KNOWN = [r'HYPERMART|hypermat|E-PO', r'LION SUPER INDO|SUPER INDO', r'PURCHASE ORDER DETAILS',
         r'LAPORAN PENERIMAAN BARANG', r'DFI RETAIL|INDY BINTARO', r'HARI HARI|PASAR SWALAYAN',
         r'GRAND ?LUCKY', r'FOODHALL', r'ALFARIA|ALFAMART', r'TIP TOP',
         r'GOODS RECEIVED NOTE|GOODS RECEIVING NOTE', r'RECEIVING SLIP ORDER',
         r'1100000382|NAMA TOKO|LAFONTE']

chain = [MS.po_primafood_pipe, MS.po_farmers_horizontal, MS.po_dfj_inline, MS.po_dfj_horizontal,
         MS.po_tiptop_po, MS.po_boots_inline, MS.po_aeon, MS.po_dfj_vertical, MS.po_boots_vertical,
         MS.po_tiptop, MS.po_mitra, MS.po_primafood, MS.po_yogya, MS.po_kalimalang]

others = []
for did, d in corpus.items():
    for p in (d.get('standard_json') or {}).get('pages', []):
        t = p.get('text') or ''
        if MS.classify(t) != 'PURCHASE_ORDER':
            continue
        rows = []
        for f in chain:
            try:
                r = f(t, p['page'])
            except Exception:
                r = []
            if r:
                rows = r; break
        if not rows:
            try:
                rows, _ = MS.po_bpb(t, p['page'])
            except Exception:
                rows = []
        if rows:
            continue
        u = t.upper()
        if any(re.search(k, u) for k in KNOWN):
            continue
        others.append((did[:8], p['page'], t))

sig = collections.Counter()
for didx, pg, t in others:
    ls = [l.strip() for l in t.splitlines() if l.strip()]
    s = ' / '.join(ls[:2])[:60]
    sig[s] += 1
print('other pages:', len(others))
for s, c in sig.most_common(28):
    print(f'{c:4d}  {s}')
ex = {}
for didx, pg, t in others:
    ls = [l.strip() for l in t.splitlines() if l.strip()]
    s = ' / '.join(ls[:2])[:60]
    ex.setdefault(s, (didx, pg, t))
out = open('/tmp/po_other_clusters.txt', 'w')
for s, (didx, pg, t) in list(ex.items())[:14]:
    out.write('=' * 20 + f' SIG[{s[:40]}] doc {didx} p{pg} ' + '=' * 20 + '\n' + t[:1700] + '\n\n')
out.close()
print('sample texts:', len(ex))
