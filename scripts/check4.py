import json, re, collections

rows = json.load(open('/tmp/sheet_now.json'))
dump = json.load(open('/tmp/dump_v5.json'))
corpus = json.load(open('/tmp/batch_map_docs.json'))

# 1) review tab lives at dump['review'], not dump['sheets']
print('dump top keys:', list(dump.keys()))
print('review in dump:', len(dump.get('review', [])), '| sheet OCR Mapping Review:', len(rows['OCR Mapping Review']) - 1)

# 2) FPj: find the 1258th faktur value
fpj = rows['Faktur Pajak']
faks = collections.Counter(r[1] for r in fpj if len(r) > 1)
weird = [f for f in faks if not re.match(r'^\d{15,18}$', str(f).strip())]
print('non-numeric faktur values:', weird[:5], '| unique numeric:', sum(1 for f in faks if re.match(r"^\d{15,18}$", str(f).strip())))

# 3) e-Faktur provenance uses Source File col (no p# prefix) — check coverage properly
ef = set()
for d in corpus.values():
    c = d.get('created') or ''
    if '2026-09-24T12:00' <= c < '2026-09-24T12:20':
        ef.add((d.get('filename') or '').rsplit('.pdf', 1)[0].strip())
in_sheet = set(r[18] for r in fpj if len(r) > 18)
missing = sorted(ef - in_sheet)
print('e-Faktur docs in window:', len(ef), '| missing from FPj Source File:', len(missing), missing[:3])

# 4) TT doc-no oddities: what are they?
tt = rows['Tanda Terima']
odd = collections.Counter(r[1] for r in tt[1:] if r[1] and not re.match(r'^\d{6,10}$', str(r[1]).strip()))
print('odd doc values:', odd.most_common(12))
r = [x for x in tt[1:] if x[1] == '62994']
print('62994 rows sample:', r[0] if r else None)
# how many distinct sources
srcs = collections.Counter(x[8].split(' ', 1)[-1][:28] for x in tt[1:] if len(x) > 8)
print('TT source files:', srcs.most_common(6))
