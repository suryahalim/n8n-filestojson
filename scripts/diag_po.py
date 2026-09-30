import json, re, sys, collections
sys.path.insert(0, '/home/suryahalim/doc-pipeline/scripts')
import knowledge as KB

rows = json.load(open('/tmp/sheet_now.json'))
corpus = json.load(open('/tmp/batch_map_docs.json'))
pl = KB.build_page_lookup(corpus)

po = rows['PO Customer']
empty = [x for x in po[1:] if not (x[0] or '').strip()]
print('empty PO rows:', len(empty))

# broad PO-candidate finders on raw page
PO_PATS = [
    (r'PO[#\s]*[:=]\s*([A-Z0-9][\w./-]{5,})', 'PO#:'),
    (r'NO[.\s]*PO\s*[:=]\s*([A-Z0-9][\w./-]{5,})', 'NO PO:'),
    (r'Nomor\s*PO\s*[:=]\s*([A-Z0-9][\w./-]{5,})', 'Nomor PO:'),
    (r'PO\s*No\.?\s*[:=]\s*([A-Z0-9][\w./-]{5,})', 'PO No:'),
    (r'\b(\d{4}\.[A-Z]{2,3}\.\d{2}\.\d{6})\b', 'dotted code'),
    (r'ORDER\s*(?:NO|NUMBER)\s*[:=\n]\s*([A-Z0-9][\w./-]{5,})', 'ORDER NO:'),
    (r'PR\s*No\s*[:=\n]\s*(\d{6,})', 'PR No:'),
    (r'\bPO\s*NUMBER\s*[:=]?\s*([A-Z0-9][\w./-]{5,})', 'PO NUMBER'),
]

by_src = collections.Counter()
cat = collections.Counter()
unfound = []
seen_pages = {}
for x in empty:
    src = x[9]
    by_src[src.split(' ', 1)[-1][:30]] += 1
    m = re.match(r'^p(\d+) (.*)$', src)
    if not m:
        unfound.append((src, 'no provenance')); continue
    fn, pn = m.group(2).strip(), int(m.group(1))
    key = (fn, pn)
    if key not in seen_pages:
        lines = pl.get(fn, {}).get(pn, [])
        t = '\n'.join(lines)
        hit = None
        for pat, label in PO_PATS:
            mm = re.search(pat, t, re.I)
            if mm:
                hit = (label, mm.group(1)[:28]); break
        seen_pages[key] = hit
    hit = seen_pages[key]
    if hit:
        cat[hit[0]] += 1
    else:
        cat['NONE'] += 1
        unfound.append((src, 'no PO token on page'))

print('\nper source doc (top):')
for k, c in by_src.most_common(12): print(f'  {c:4}  {k}')
print('\nresolvable by pattern:')
for k, c in cat.most_common(): print(f'  {k}: {c}')
print('\nunfound sample:', unfound[:8])
