import json, re, collections, sys
sys.path.insert(0, '/home/suryahalim/doc-pipeline/scripts')

corpus_all = json.load(open('/tmp/batch_map_docs.json'))
wins = [('2026-09-24T12:00', '2026-09-24T12:20'), ('2026-09-24T13:00', '2026-09-26T23:59')]
corpus = {k: v for k, v in corpus_all.items()
          if any((not s or (v.get('created') or '') >= s) and (not u or (v.get('created') or '') < u) for s, u in wins)}

BRANDS = {
    'AEON': r'\bAEON\b', 'HARI HARI': r'HARI HARI|PASAR SWALAYAN', 'LION': r'LION SUPER INDO|SUPER INDO',
    'TIP TOP': r'\bTIP TOP\b', 'ASTRO': r'ASTRO|ASTRONAUTS', 'MIDI': r'\bMIDI\b',
    'FOODHALL': r'FOODHALL', 'SUPRA': r'SUPRA BOGA', 'MITRA': r'MITRA BELANJA',
    'ALFARI': r'ALFARIA|ALFAMIDI', 'HERO': r'HERO RETAIL|heroretailplatform|HEROTOKOGROSIR',
    'BERKAH': r'PT BERKAH', 'GRAMEDIA': r'GRAMEDIA|ASRI MEDIA', 'KAGE': r'KAGE DWIJAYA',
    'HYPERMART': r'HYPERMART|hypermat', 'DUTA BUAH': r'DUTA BUAH', 'GRANDLUCKY': r'GRAND ?LUCKY',
    'VICTORY': r'VICTORY RETAILINDO', 'PAPAYA': r'papaya FRESH|PAPAYA FRESH',
    'MATAHARI': r'MATAHARI GRAHA', 'PRIMAFOOD': r'PRIMAFOOD', 'DFI': r'DFI RETAIL|INDY BINTARO',
    'RAMAYANA': r'RAMAYANA', 'SAMS': r'SAMS', 'YOGYA': r'YOGYA', 'FARMERS': r'FARMERS|RANCH|SCS MARK',
}
SELF = re.compile(r'SARANA\s+ABADI|SAHANA|S?ABADI\s+MAKMUR|KIRANA ABADI|MAJU ABADI MAKMUR|BERSAMA[.,]? ?PT', re.I)
PT_RE = re.compile(r'\bPT\.?\s*([A-Z][A-Z .,&\'()\-]{4,48}?)(?=,|\n|\s{2}|TBK|\(TBK|$)', re.M)

pair = collections.defaultdict(collections.Counter)
for did, d in corpus.items():
    for p in (d.get('standard_json') or {}).get('pages', []):
        t = p.get('text') or ''
        u = t.upper()
        if not (re.search(r'PURCHASE ORDER|\bNO PO\b|PO ?Number|PO#', u) or 'LAPORAN PENERIMAAN' in u or 'PEMBERIAN ORDER' in u):
            continue
        pts = []
        for m in PT_RE.finditer(t):
            name = re.sub(r'\s+', ' ', m.group(0)).strip().rstrip(',.')
            if SELF.search(name) or len(name) < 8:
                continue
            pts.append(name.upper())
        for b, pat in BRANDS.items():
            if re.search(pat, u):
                for x in pts:
                    pair[b][x] += 1

issuers = {}
for b, c in pair.items():
    if not c:
        continue
    top, n = c.most_common(1)[0]
    tot = sum(c.values())
    if n >= 3 and n >= tot * 0.5:
        issuers[b] = {'pt': top, 'n': n, 'tot': tot}
    else:
        issuers[b] = {'pt': '', 'top': top, 'n': n, 'tot': tot}
json.dump(issuers, open('/tmp/issuers_boot.json', 'w'), indent=1)
for b, v in sorted(issuers.items()):
    print(f"{b:12s} n={v['n']:4d}/{v['tot']:4d} -> {v['pt'] or '(UNPROVEN: '+v['top'][:40]+')'}")
