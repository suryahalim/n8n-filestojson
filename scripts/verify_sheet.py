import json, re, collections, subprocess, sys

rows = json.load(open('/tmp/sheet_now.json'))
corpus = json.load(open('/tmp/batch_map_docs.json'))
dump = json.load(open('/tmp/dump_v5.json'))

def n2(s):
    s = str(s).strip().replace(' ', '')
    if not s: return None
    if re.fullmatch(r'\d{1,3}(\.\d{3})+(,\d+)?', s): s = s.replace('.', '').replace(',', '.')
    elif re.fullmatch(r'\d+,\d+', s): s = s.replace(',', '.')
    else: return None
    try: return float(s)
    except ValueError: return None

PROB = []
def check(name, ok, detail=''):
    print(('PASS ✓' if ok else 'FAIL ✗'), name, detail)
    if not ok: PROB.append(name + ' ' + detail)

# ---------- 0. sheet vs last dump (write integrity) ----------
for tab in ['Faktur Penjualan','PO Customer','Tanda Terima','Surat Jalan','Faktur Pajak','OCR Mapping Review']:
    d = len(dump['sheets'].get(tab, []))
    s = len(rows.get(tab, [])) - 1
    check(f'write-integrity {tab}', d == s, f'dump {d} vs sheet {s}')

# ---------- 1. Faktur Pajak: per-doc arithmetic from the SHEET itself ----------
fpj = rows['Faktur Pajak']
per = collections.defaultdict(lambda: dict(items=0, sumj=0.0, hj=None, dpp=None, ppn=None))
for r in fpj:
    if len(r) < 20: continue
    key = (r[0] or '') + '|' + (r[1] or '')
    j = n2(r[12]); hj = n2(r[12])
    d = per[key]
    if r[9] and j:
        d['items'] += 1; d['sumj'] += j
    if hj and d['hj'] is None: d['hj'] = hj
print()
doc_cnt = collections.Counter()
for r in fpj:
    if len(r) > 2:
        m = re.match(r'^0\d\.\d{3}\.\d{3}\.\d{3}\.\d{3}\.\d{3}-\d{3}\.\d{3}$|^\d{15,16}', str(r[2] or ''))
# simpler: count distinct faktur numbers in Billing/KodeSeri col
faks = set()
for r in fpj:
    if len(r) > 1 and r[1]: faks.add(r[1])
check('FPj unique faktur == 1257', len(faks) == 1257, f'found {len(faks)}')
# sum items vs Harga Jual per faktur
bad = []
byf = collections.defaultdict(list)
for r in fpj:
    if len(r) < 19: continue
    if r[1]: byf[r[1]].append(r)
mismatch = 0
for f, rs in byf.items():
    itemrows = [r for r in rs if r[9] and n2(r[12])]
    hjrows = [n2(r[17]) for r in rs if n2(r[17])]
    if not itemrows or not hjrows: continue
    s = sum(n2(r[12]) for r in itemrows)
    hj = hjrows[0]
    # header row repeats totals; compare Σitem to HargaJual of header
    hdr = [r for r in rs if r[12] and not r[9]]
    if hdr:
        hj = n2(hdr[0][12]) or hj
    if hj and abs(s - hj) > max(500, hj * 0.001):
        mismatch += 1
        if len(bad) < 4: bad.append((f, round(s), round(hj)))
check('FPj Σitem=footer per faktur', mismatch == 0, f'mismatch {mismatch} {bad if bad else ""}')

# ---------- 2. FP PASS-LEARNED really pass under promoted sem ----------
sys.path.insert(0, '/home/suryahalim/doc-pipeline/scripts')
import knowledge as KB
pl = KB.build_page_lookup(corpus)
fp = rows['Faktur Penjualan']
vkb = KB.load_kb()
ok_pl = bad_pl = 0
for r in fp:
    if len(r) < 18 or r[17] != 'PASS-LEARNED': continue
    h, j = n2(r[5]), n2(r[11])
    mq = KB.QTY_RE.match((r[4] or '').replace(' ', ''))
    isi = KB.isi_per_carton(r[2])
    if not (h and j and mq and isi):
        bad_pl += 1; continue
    A, B = int(mq.group(1)), int(mq.group(2))
    if abs((A * isi + B) * h - j) <= max(3, j * 0.02): ok_pl += 1
    else: bad_pl += 1
check('PASS-LEARNED rows verify under cart*isi+pcs', bad_pl == 0, f'ok {ok_pl} bad {bad_pl}')

# ---------- 3. provenance integrity: p# exists in real doc ----------
allprov = []
for tab, rs in rows.items():
    for r in rs[1:]:
        sp = next((c for c in reversed(r) if isinstance(c, str) and re.match(r'^p\d+ ', c)), None)
        if sp: allprov.append(sp)
fn_ok = { (d.get('filename') or '').rsplit('.pdf',1)[0].strip(): d for d in corpus.values() }
badprov = 0; maxpg = {}
for sp in allprov:
    m = re.match(r'^p(\d+) (.*)$', sp)
    if not m: badprov += 1; continue
    pn, fn = int(m.group(1)), m.group(2).strip()
    d = fn_ok.get(fn)
    if not d: badprov += 1; continue
    np_ = len((d.get('standard_json') or {}).get('pages') or [])
    if pn > np_: badprov += 1
check('provenance pages within real doc', badprov == 0, f'{len(allprov)} refs, bad {badprov}')

# ---------- 4. vendor rules still enforced ----------
self_re = re.compile(r'SARANA\s*ABADI|ABADI\s*MAKMUR', re.I)
junk_re = re.compile(r'^(PT\.?\s*)?(ORDER DATE|ADDRESS|NO\.?$|YANG|DAN )', re.I)
po = rows['PO Customer']
badv = [r for r in po if len(r) > 1 and (self_re.search(str(r[1])) or junk_re.search(str(r[1]).strip()))]
check('PO vendor: no self/junk', len(badv) == 0, str([r[1][:20] for r in badv[:3]]))
fpe = [r for r in fp if len(r) > 1 and self_re.search(str(r[1] or ''))]
# FP tab has no vendor column (SOR-based) — skip
# empty vendor still?
empt = sum(1 for r in po[1:] if not (r[1] if len(r) > 1 else ''))
print('  info: PO empty-vendor rows:', empt, '/', len(po) - 1)
vend_by_po = {}
for r in po[1:]:
    if len(r) > 10 and r[1]: vend_by_po.setdefault(r[0], r[1])
rec = sum(1 for r in po[1:] if len(r) > 10 and not r[1] and r[0] in vend_by_po)
print('  info: of which recoverable via learned memory:', rec)

# ---------- 5. data tabs: no empty / placeholder rows ----------
for tab, rs in rows.items():
    if tab == 'Dokumen Pelunasan' or not rs: continue
    empty = sum(1 for r in rs[1:] if not any(str(c).strip() for c in r[:-2]))
    check(f'{tab}: no data-empty rows', empty == 0, f'{empty}')

# ---------- 6. coverage: every non-split doc in window represented ----------
win = set()
for d in corpus.values():
    c = d.get('created') or ''
    if '2026-09-24T12:00' <= c < '2026-09-24T12:20' or '2026-09-24T13:00' <= c < '2026-09-26T23:59':
        win.add((d.get('filename') or '').rsplit('.pdf',1)[0].strip())
mapped = set()
for tab, rs in rows.items():
    for r in rs[1:]:
        sp = next((c for c in reversed(r) if isinstance(c, str) and re.match(r'^p\d+ ', c)), None)
        if sp:
            m = re.match(r'^p\d+ (.*)$', sp)
            if m: mapped.add(m.group(1).strip())
missing = sorted(w for w in win if w not in mapped)
check('all window docs appear in sheet (any tab incl review)', len(missing) == 0, f'missing {missing[:6]}')

# ---------- 7. TT/SJ value sanity ----------
tt = rows['Tanda Terima']
baddoc = [r for r in tt[1:] if r[1] and not re.match(r'^\d{6,10}$', str(r[1]).strip())]
check('TT document-no format', len(baddoc) == 0, f'{len(baddoc)} odd, e.g. {[r[1] for r in baddoc[:4]]}')
sj = rows['Surat Jalan']
badsj = [r for r in sj[1:] if r[0] and not re.match(r'^\d+$', str(r[0]).strip())]
check('SJ no format numeric', len(badsj) == 0, f'{len(badsj)} odd {[r[0] for r in badsj[:4]]}')
badd = [r for r in sj[1:] if r[1] and not re.match(r'^\d{2}/\d{2}/\d{4}$', str(r[1]).strip())]
print('  info: SJ odd dates:', [r[1] for r in badd[:5]])

print()
print('===== TOTAL PROBLEMS:', len(PROB))
for p in PROB: print(' -', p)
