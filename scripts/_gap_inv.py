import json, re, sys, collections
sys.path.insert(0, '/home/suryahalim/doc-pipeline/scripts')
import map_sheets as MS

corpus = json.load(open('/tmp/batch_map_docs.json'))
PO_KEYS = re.compile(r'PURCHASE ORDER|\bNO PO\b|PO Number|PO#|Nomor P\.?O|P\.O No|ORDER NUMBER', re.I)

# simulate build()'s PO chain per page -> which PO pages yield 0 rows
chain = [MS.po_primafood_pipe, MS.po_farmers_horizontal, MS.po_dfj_inline, MS.po_dfj_horizontal,
         MS.po_tiptop_po, MS.po_boots_inline, MS.po_aeon, MS.po_dfj_vertical, MS.po_boots_vertical,
         MS.po_tiptop, MS.po_mitra, MS.po_primafood, MS.po_yogya, MS.po_kalimalang]

gap = []
handled = 0
for did, d in corpus.items():
    for p in (d.get('standard_json') or {}).get('pages', []):
        t = p.get('text') or ''
        cat = MS.classify(t)
        if cat != 'PURCHASE_ORDER' and not PO_KEYS.search(t.upper()):
            continue
        try:
            rows = next(iter([f(t, p['page']) for f in chain if f(t, p['page'])]), [])
        except Exception:
            rows = []
        # bpb fallback
        if not rows:
            try:
                bpb, _ = MS.po_bpb(t, p['page'])
                rows = bpb
            except Exception:
                pass
        if rows:
            handled += 1
        else:
            head = ' / '.join([l.strip() for l in t.splitlines() if l.strip()][:3])[:90]
            gap.append((head, len(t)))
print('PO-marker pages handled:', handled, '| GAP (0 rows):', len(gap))
sig = collections.Counter(h[:48] for h, n in gap)
for s, c in sig.most_common(30):
    print(f'{c:4d}  {s}')
json.dump(gap, open('/tmp/po_gap.json', 'w'))
