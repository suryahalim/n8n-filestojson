import json, re, sys, collections
sys.path.insert(0, '/home/suryahalim/doc-pipeline/scripts')
import map_sheets as MS

corpus_all = json.load(open('/tmp/batch_map_docs.json'))
wins = [('2026-09-24T12:00', '2026-09-24T12:20'), ('2026-09-24T13:00', '2026-09-26T23:59')]
corpus = {k: v for k, v in corpus_all.items()
          if any((not s or (v.get('created') or '') >= s) and (not u or (v.get('created') or '') < u) for s, u in wins)}

chain = [MS.po_primafood_pipe, MS.po_farmers_horizontal, MS.po_dfj_inline, MS.po_dfj_horizontal,
         MS.po_tiptop_po, MS.po_boots_inline, MS.po_aeon, MS.po_dfj_vertical, MS.po_boots_vertical,
         MS.po_tiptop, MS.po_mitra, MS.po_primafood, MS.po_yogya, MS.po_kalimalang]

sig_re = {
    'slip321': r'SLIP TYPE',
    'taxrate': r'Jangan Mengirim Barang',
    'aeon': r'\bAEON\b',
    'dutabuah': r'DUTA BUAH',
    'gramedia': r'SRI MEDIA|ASRI MEDIA|GRAMEDIA',
    'pono_gen': r'^PO No',
    'kage': r'KAGE DWIJAYA',
    'hero': r'heroretailplatform|HERO',
    'victory': r'VICTORY RETAILINDO',
    'papaya': r'papaya|FRESH GALLERY',
    'berkah': r'BERKAH',
    'matahari': r'Matahari Graha',
    'scanned': r'Scanned PO',
    'foodhall': r'FOODHALL',
}
got = collections.defaultdict(list)
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
        if any(re.search(k, u) for k in [r'HYPERMART|hypermat|E-PO', r'LION SUPER INDO|SUPER INDO',
                r'PURCHASE ORDER DETAILS', r'LAPORAN PENERIMAAN', r'PENERIMAAN BARANG', r'HARI HARI|PASAR SWALAYAN',
                r'GRAND ?LUCKY', r'ALFARIA|ALFAMART', r'TIP TOP', r'1100000382|LAFONTE|NAMA TOKO',
                r'GOODS RECEIV']):
            continue
        for name, pat in sig_re.items():
            if re.search(pat, t, re.I) and len(got[name]) < 1:
                got[name].append((did[:8], p['page'], t))
out = open('/tmp/po_other_sigs.txt', 'w')
for k, lst in got.items():
    for didx, pg, t in lst:
        out.write('=' * 22 + f' [{k}] doc {didx} p{pg} ' + '=' * 22 + '\n' + t[:1600] + '\n\n')
out.close()
print('captured:', sorted(got))
