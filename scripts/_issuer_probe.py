import json, re, collections
corpus_all = json.load(open('/tmp/batch_map_docs.json'))
wins = [('2026-09-24T12:00', '2026-09-24T12:20'), ('2026-09-24T13:00', '2026-09-26T23:59')]
corpus = {k: v for k, v in corpus_all.items()
          if any((not s or (v.get('created') or '') >= s) and (not u or (v.get('created') or '') < u)
                 for s, u in wins)}

issuer_re = re.compile(r'\bPT\.?\s*[A-Z][A-Za-z .,&\'()\-]{3,48}?\b(?:TBK|Tbk|\(TBK\))?\b')
vc_re = re.compile(r'(?:VENDOR|No\.?\s*Supplier|Kode\s*Supplier|Supplier)\s*[:\s]*\n?\s*\(?([A-Z0-9][A-Z0-9./]{2,20})\)?:?\s', re.I)
ppn_re = re.compile(r'(?:PPN|PPn)\s*(?::|\s)\s*(11|12|1\.1|11\.00|1\.10|12\.00)(?:\s*%|%)', re.I)

def norm_pt(s):
    s = re.sub(r'\s+', ' ', s).strip().upper().rstrip('.,')
    return s

samp = collections.Counter(); vc_samp = collections.Counter(); ppn_samp = collections.Counter()
brand_to_pt = collections.defaultdict(collections.Counter)
BRANDS = ['HYPERMART', 'HARI HARI', 'GRAND ?LUCKY', 'SUPER INDO|LION', 'TIP TOP', 'AEON',
          'MITRA BELANJA', 'MIDI', 'ALFARIA|ALFAMIDI|ALFAMART', 'ASTRO', 'FOODHALL', 'SUPRA BOGA',
          'DUTA BUAH', 'RAMAYANA', 'HERO', 'Berkah|BERKAH', 'PRIMAFOOD', 'INDOGROSIR|GROSIR']
for did, d in corpus.items():
    for p in (d.get('standard_json') or {}).get('pages', []):
        t = p.get('text') or ''
        u = t.upper()
        if 'PURCHASE ORDER' not in u and not re.search(r'\bNO PO\b|PO Number|PO#|Nomor P\.?O', u):
            continue
        pts = [norm_pt(m.group(0)) for m in issuer_re.finditer(t)]
        pts = [x for x in pts if 'SARANA ABADI' not in x and 'SAHANA' not in x and len(x) > 6]
        if pts:
            samp['has-PT'] += 1
            for x in sorted(set(pts))[:3]:
                samp[x[:40]] += 0
        else:
            samp['no-PT'] += 1
        m = vc_re.search(t)
        if m:
            vc_samp[m.group(1)[:18]] += 1
        mp = ppn_re.search(t)
        if mp:
            ppn_samp[mp.group(1)] += 1
        # brand co-occurrence -> PT names on same page
        for b in BRANDS:
            if re.search(b, u):
                for x in pts[:4]:
                    brand_to_pt[re.sub(r'\|.*', '', b)][x] += 1
print('PT-name availability on PO pages:', {k: samp[k] for k in ('has-PT', 'no-PT') if k in samp})
print('vendor code samples (top15):', vc_samp.most_common(15))
print('PPN prints:', dict(ppn_samp))
print()
for b, c in brand_to_pt.items():
    print(b, '->', [f'{n}×{x[:38]}' for x, n in c.most_common(3)])
