import json, re, collections

rows = json.load(open('/tmp/sheet_now.json'))

def n2(s):
    s = str(s).strip().replace(' ', '')
    if not s: return None
    if re.fullmatch(r'\d{1,3}(\.\d{3})+(,\d+)?', s): s = s.replace('.', '').replace(',', '.')
    elif re.fullmatch(r'\d+,\d+', s): s = s.replace(',', '.')
    else:
        return None
    try: return float(s)
    except Exception: return None

print('===== FAKTUR PENJUALAN: hypothesis qty = karton / pcs, harga per pcs =====')
fp = rows['Faktur Penjualan']
tot = ok = ok_alt = fail = noisi = 0
fails = []
for r in fp[1:]:
    if len(r) < 18 or r[17] == 'SUMMARY': continue
    h, j = n2(r[5]), n2(r[11])
    if None in (h, j): continue
    tot += 1
    mq = re.match(r'(\d+)\s*/\s*(\d+)', r[4].replace(' ', ''))
    mi = re.match(r'^(\d+)\s*[Xx]', r[2].replace(' ', ''))
    if mq and mi:
        q1, q2, isi = int(mq.group(1)), int(mq.group(2)), int(mi.group(1))
        exp = (q1 * isi + q2) * h
        if abs(exp - j) <= max(3, j * 0.004): ok += 1
        elif abs(q1 * isi * h - j) <= max(3, j * 0.004): ok_alt += 1
        else:
            fail += 1
            if len(fails) < 8: fails.append((r[1], r[2], r[3][:30], r[4], r[5], r[11], round(exp)))
    elif mq:
        q1, q2 = int(mq.group(1)), int(mq.group(2))
        if abs(q1 * h - j) <= 3 or abs(q2 * h - j) <= 3: ok += 1
        else: fail += 1
    else:
        noisi += 1
print(f'checked {tot}: PASS(qty*isi*harga) {ok} | pass-variant {ok_alt} | FAIL {fail} | unparseable {noisi}')
for f in fails: print('  FAIL:', f)

print()
print('===== TANDA TERIMA: column shift check =====')
tt = rows['Tanda Terima']
print('header:', tt[0])
lens = collections.Counter(len(r) for r in tt[1:])
print('row length dist:', dict(lens))
# where does status live?
stat_col = collections.Counter()
for r in tt[1:]:
    for i, c in enumerate(r):
        if str(c).strip().upper().startswith(('MAPPED', 'REVIEW', 'OCR_', 'PASS', 'DUP')): stat_col[i] += 1
print('status found at col idx:', dict(stat_col))
sh = [r for r in tt[1:] if len(r) == 9]
print('rows with 9 cols (shifted?):', len(sh), 'sample:', sh[0] if sh else None)
docok = sum(1 for r in tt[1:] if re.match(r'^\d{8}', str(r[1] or '')))
print('col1 looks like doc-no in:', docok, '/', len(tt) - 1)

print()
print('===== PO CUSTOMER: empty vendor rows =====')
po = rows['PO Customer']
ev = [r for r in po[1:] if not (r[1] if len(r) > 1 else '')]
print('empty-vendor rows:', len(ev))
src = collections.Counter((r[9].split(' ', 1)[0] if len(r) > 9 else '?') + ' ' + (r[9].split(' ', 1)[-1][:24] if len(r) > 9 else '') for r in ev)
print('their source files:', src.most_common(8))

print()
print('===== FAKTUR PAJAK: re-verify with correct columns =====')
fpj = rows['Faktur Pajak']
badnp = [r for r in fpj[1:] if len(r) > 7 and not re.match(r'^\d{2}\.\d{3}\.\d{3}\.\d{3}\.\d{3}-?\d{0,3}\.?\d{0,3}', str(r[7] or ''))]
print('NPWP-pembeli not standard fmt:', len(badnp))
for r in badnp[:5]: print('   ', r[7], '|', r[8][:24])
c_npwp = collections.Counter(len(re.sub(r'\D', '', r[7])) for r in fpj[1:] if len(r) > 7)
print('NPWP digit-length dist:', dict(c_npwp))

print()
print('===== SURAT JALAN: dates & numbers =====')
sj = rows['Surat Jalan']
dt = collections.Counter(r[1] for r in sj[1:])
print('date values:', dict(list(dt.items())[:10]))
snum = [r[0] for r in sj[1:]]
print('short DO nos (<4 chars):', [x for x in snum if len(str(x)) < 4][:12])
