import json, re, sys, collections
sys.path.insert(0, '/home/suryahalim/doc-pipeline/scripts')
import knowledge as KB

d = json.load(open('/tmp/dump_v8.json'))['sheets']['PO Customer']
corpus = json.load(open('/tmp/batch_map_docs.json'))
pl = KB.build_page_lookup(corpus)

empty = [x for x in d if not str(x[0]).strip()]
print('still-empty rows:', len(empty))
for x in empty:
    print('  ', x[9], '| vendor:', x[1][:34], '| status:', x[10])

# audit backfilled rows: PO must genuinely belong to the row's vendor page-group
bf = [x for x in d if 'PO-BACKFILL' in x[10] or 'PO-INHERIT' in x[10]]
print('\nbackfilled rows:', len(bf))

PO_LABS = [r'PO[#\s]*[:=]\s*([A-Z0-9][A-Z0-9./_-]{5,27})',
           r'Purchase Order No[.\s]*[:=]?\s+([A-Z0-9][A-Z0-9./_-]{5,27})',
           r'PURCHASE\s+ORDER\s*\n+\s*(\d{6,12})\b',
           r'NO[.\s]*PO\s*[:=]\s*([A-Z0-9][A-Z0-9./_-]{5,27})',
           r'Nomor\s*PO\s*[:=]\s*([A-Z0-9][A-Z0-9./_-]{5,27})',
           r'PO\s*No\.?\s*[:=]\s*([A-Z0-9][A-Z0-9./_-]{5,27})',
           r'PO\s*NUMBER\s*[:=]?\s*([A-Z0-9][A-Z0-9./_-]{5,27})',
           r'ORDER\s*NO\.?\s*[:=]\s*([A-Z0-9][A-Z0-9./_-]{6,27})',
           r'PR\s*No\s*[:=\n]\s*(\d{6,12})',
           r'[:=]\s*(\d{10})\b(?=[^\n]*Purch\.?\s*Grp)']
MONTH_RE = re.compile(r'JAN|FEB|MAR|APR|MEI|JUN|JUL|AGU|SEP|OKT|NOV|DES|OCT|DEC', re.I)

def page_po_label(lines):
    t = '\n'.join(lines)
    for rx in PO_LABS:
        for mm in re.finditer(rx, t, re.I):
            v = mm.group(1).strip().rstrip('.,')
            if re.search(r'\d{5,}', v) and not MONTH_RE.search(v):
                return v[:28]
    return ''

# 1) PAGE-tagged rows: same page's own label must equal the written PO
bad_page = 0
for x in bf:
    if 'PO-BACKFILL' not in x[10]:
        continue
    m = re.match(r'^p(\d+) (.*)$', x[9]); fn, pn = m.group(2).strip(), int(m.group(1))
    own = page_po_label(pl.get(fn, {}).get(pn, []))
    if own != x[0]: bad_page += 1; print('  MISMATCH page:', x[9], x[0], '!=', own)
print('PAGE backfills self-consistent:', sum(1 for x in bf if 'PO-BACKFILL' in x[10]) - bad_page)

# 2) INHERIT rows: source page must show the vendor (guard) AND the inherited PO's origin
#    page must be within 3 earlier and belong to a page where vendor label also appears
bad_inh = 0
for x in bf:
    if 'PO-INHERIT' not in x[10]:
        continue
    m = re.match(r'^p(\d+) (.*)$', x[9]); fn, pn = m.group(2).strip(), int(m.group(1))
    t = '\n'.join(pl.get(fn, {}).get(pn, []))
    v = re.sub(r'[.]', '', x[1].upper())[:12]
    if v and v not in re.sub(r'[.]', '', t.upper()):
        bad_inh += 1
        print('  VENDOR-NOT-ON-PAGE:', x[9], repr(x[1][:20]))
print('INHERIT vendor-guard violations:', bad_inh)

# 3) one PO number should map to exactly one vendor across the tab
pv = collections.defaultdict(set)
for x in d:
    if x[0] and x[1]:
        pv[x[0]].add(x[1])
conflict = {k: v for k, v in pv.items() if len(v) > 1}
print('PO->vendor conflicts:', len(conflict))
for k, v in list(conflict.items())[:5]:
    print('  ', k, '->', list(v)[:3])

# 4) GrandLucky spot check (user example)
gl = [x for x in d if '72009500' in x[9]]
print('\nGrandLucky 72009500 rows:', len(gl), '| distinct PO:', sorted(set(x[0] for x in gl))[:6])
print('sample:', [str(c)[:34] for c in gl[0][:11]])
