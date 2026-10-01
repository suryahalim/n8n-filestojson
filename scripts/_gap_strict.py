import json, re, sys, collections
sys.path.insert(0, '/home/suryahalim/doc-pipeline/scripts')
import map_sheets as MS

corpus = json.load(open('/tmp/batch_map_docs.json'))
chain = [MS.po_primafood_pipe, MS.po_farmers_horizontal, MS.po_dfj_inline, MS.po_dfj_horizontal,
         MS.po_tiptop_po, MS.po_boots_inline, MS.po_aeon, MS.po_dfj_vertical, MS.po_boots_vertical,
         MS.po_tiptop, MS.po_mitra, MS.po_primafood, MS.po_yogya, MS.po_kalimalang]

gap = collections.Counter(); handled = 0; fam_pages = collections.defaultdict(list)
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
            handled += 1
            continue
        u = t.upper()
        if re.search(r'\bHYPERMART|\bhypermat\b|E-PO', u): fam = 'hypermart-epo'
        elif re.search(r'LION SUPER INDO|SUPER INDO', u): fam = 'lion'
        elif 'PURCHASE ORDER DETAILS' in u: fam = 'astro'
        elif 'LAPORAN PENERIMAAN BARANG' in u: fam = 'midi-lpb'
        elif re.search(r'DFI RETAIL|INDY BINTARO', u): fam = 'dfi-indy'
        elif re.search(r'HARI HARI|PASAR SWALAYAN', u): fam = 'harihari'
        elif re.search(r'GRAND ?LUCKY', u): fam = 'grandlucky'
        elif re.search(r'FOODHALL', u): fam = 'foodhall'
        elif re.search(r'ALFARIA|ALFAMART', u): fam = 'alfamart-pof'
        elif re.search(r'TIP TOP', u): fam = 'tiptop-po'
        elif re.search(r'GOODS RECEIVED NOTE|GOODS RECEIVING NOTE', u): fam = 'grn-recv'
        elif re.search(r'RECEIVING SLIP ORDER', u): fam = 'recv-slip'
        else: fam = 'other'
        gap[fam] += 1
        if len(fam_pages[fam]) < 2:
            fam_pages[fam].append((did[:8], p['page'], t))
print('PO-classified pages handled:', handled, '| gap:', sum(gap.values()))
for f, c in gap.most_common():
    print(f'  {c:4d}  {f}')
out = open('/tmp/po_families.txt', 'w')
for f, lst in fam_pages.items():
    for didx, pg, t in lst:
        out.write('=' * 25 + f' {f} doc {didx} p{pg} ' + '=' * 25 + '\n' + t[:2200] + '\n\n')
out.close()
json.dump({k: v for k, v in gap.items()}, open('/tmp/po_gap_families.json', 'w'))
