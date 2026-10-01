import json, re, collections

c = json.load(open('/tmp/batch_map_docs.json'))
wins = [('2026-09-24T12:00', '2026-09-24T12:20'), ('2026-09-24T13:00', '2026-09-26T23:59')]
corpus = {k: v for k, v in c.items()
          if any(a <= (v.get('created') or '')[:16] <= b for a, b in wins)}

BRANDS = {
    'HYPERMART': r'HYPERMART',
    'GRAND LUCKY': r'GRAND\s*LUCKY',
    'RANCH MARKET': r'RANCH MARKET',
    'FARMERS MARKET': r'FARMERS? MARKET',
    'SUPERHERO': r'\bSUPERHERO|HERO MARKET',
    'FOOD STATION': r'FOOD STATION',
    'YOGYA': r'\bYOGYA\b',
    'METRO AISAN': r'METRO\s?AI?SAN',
    'PLAZA': r'\bPLAZA\s*(INDONESIA|SEMARANG)?\b',
}
PT_RE = re.compile(r'PT\.?\s*[A-Z][A-Za-z .,&\'()\-]{4,48}?(?:TBK|\(TBK\))?(?=\s*(?:$|[,:(\n])|\s{2,})', re.M)
SELF = re.compile(r'SARANA|SAHANA|ABADI MAKMUR|MAJU ABADI', re.I)

pairs = collections.defaultdict(collections.Counter)
n_pages = 0
for did, d in corpus.items():
    for p in (d.get('standard_json') or {}).get('pages', []):
        t = p.get('text') or ''
        tu = t.upper()
        if 'PURCHASE ORDER' not in tu and not re.search(r'NO PO\s*:|PO ?Number|PO#|Nomor P?\.?O', tu):
            continue
        n_pages += 1
        pts = []
        for m in PT_RE.finditer(tu):
            s = re.sub(r'\s+', ' ', m.group(0)).strip().rstrip('.,')
            s = re.sub(r'\s+(SUDAH|PLEASE|SEND|TO|FROM|FOR|WITH)\b.*$', '', s)
            if not SELF.search(s) and len(s) >= 9:
                pts.append(s)
        for brand, rx in BRANDS.items():
            if re.search(rx, tu):
                for s in pts:
                    pairs[brand][s] += 1

print('PO pages scanned:', n_pages)
for b, cnt in pairs.items():
    tot = sum(cnt.values())
    if tot:
        top, n = cnt.most_common(1)[0]
        if n >= 3:
            print(f'{b}: {n}/{tot} pages -> {top} | others: {cnt.most_common(4)}')
