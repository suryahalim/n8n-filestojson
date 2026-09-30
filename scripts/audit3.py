import json, re, collections

rows = json.load(open('/tmp/sheet_now.json'))

def n2(s):
    s = str(s).strip().replace(' ', '')
    if not s: return None
    if re.fullmatch(r'\d{1,3}(\.\d{3})+(,\d+)?', s): s = s.replace('.', '').replace(',', '.')
    elif re.fullmatch(r'\d+,\d+', s): s = s.replace(',', '.')
    else: return None
    try: return float(s)
    except Exception: return None

print('== FP: nested-packaging hypothesis (qty * isi1 * isi2 ... * harga) ==')
fp = rows['Faktur Penjualan']
ok_nested = 0; fail_real = 0; zero_jml = 0; fails = []
for r in fp[1:]:
    if len(r) < 18 or r[17] == 'SUMMARY': continue
    h, j = n2(r[5]), n2(r[11])
    if h is None or j is None: continue
    if j == 0:
        zero_jml += 1; continue
    mq = re.match(r'(\d+)\s*/\s*(\d+)', r[4].replace(' ', ''))
    nums = [int(x) for x in re.findall(r'\d+', r[2])]
    if not mq or not nums: continue
    q1, q2 = int(mq.group(1)), int(mq.group(2))
    base = q1 * q2 if (q1 and q2 and q2 < 1000 and q1 * q2 > q1) else (q1 * q2 if q2 else q1)
    for mult in (q1, q1 * (max(nums) if q2 == 0 else 1)):
        pass
    # try: qty_total = q1 * product(all pack nums) when q2==0, else q1*prod + q2
    prod = 1
    for x in nums: prod *= x
    cand = [q1 * prod * h, (q1 * prod + q2) * h, q1 * h, (q1 * nums[0]) * h if nums else 0, ((q1 or 1) * (nums[0] if nums else 1) * (nums[1] if len(nums) > 1 else 1)) * h]
    if any(abs(c - j) <= max(3, j * 0.005) for c in cand): ok_nested += 1
    else:
        fail_real += 1
        if len(fails) < 10: fails.append((r[1][:16], r[2], r[3][:26], r[4], r[5], r[11]))
print(f'pass-with-nested: {ok_nested} | jumlah==0 rows: {zero_jml} | still-fail: {fail_real}')
for f in fails: print('  FAIL:', f)

print()
print('== FP: jumlah=0 rows -> do they belong to SOR whose same item appears elsewhere with real jumlah? ==')
z = [r for r in fp[1:] if len(r) > 17 and r[17] != 'SUMMARY' and r[11] == '0,00']
print('count jumlah=0:', len(z))
by_sor = collections.defaultdict(list)
for r in fp[1:]:
    if len(r) > 17: by_sor[r[1]].append(r)
dups = 0
for r in z[:500]:
    same = [x for x in by_sor.get(r[1], []) if x is not r and x[3] == r[3] and n2(x[11])]
    if same: dups += 1
print(f'zero-rows whose same SOR+item exists with real jumlah elsewhere: {dups} (candidate for dedup)')

print()
print('== FPJ: NPWP triple format breakdown ==')
fpj = rows['Faktur Pajak']
trip = sum(1 for r in fpj[1:] if len(r) > 7 and str(r[7]).count('/') == 2)
print('NPWP-pembeli with 3 slash-parts:', trip, '/', len(fpj) - 1)
r = fpj[1]
print('sample row1: seller=', r[3][:60])
print('           pembeli=', r[7][:60])
def digits_parts(s):
    return [re.sub(r'\D', '', p) for p in str(s).split('/')]
def npwp_canon(p):
    d = re.sub(r'\D', '', p)
    d = d.lstrip('0')
    return d[-15:] if len(d) >= 15 else d[-15:] if d else ''
same = 0; diff = 0; ex = []
for r in fpj[1:]:
    if len(r) < 8: continue
    parts = digits_parts(r[7])
    if len(parts) >= 2:
        a = npwp_canon(parts[0]); b = npwp_canon(parts[1])
        if a == b: same += 1
        else:
            diff += 1
            if len(ex) < 5: ex.append(r[7][:80])
print('p1 vs p2 canon-equal:', same, '| different:', diff)
for e in ex: print('   DIFF:', e)

print()
print('== PO: empty-vendor rows — same PO elsewhere WITH vendor? ==')
po = rows['PO Customer']
vend = {}
for r in po[1:]:
    if len(r) > 10 and r[1]:
        vend.setdefault(r[0], r[1])
ev = [r for r in po[1:] if len(r) > 10 and not r[1]]
rec = sum(1 for r in ev if r[0] in vend)
print(f'empty-vendor rows: {len(ev)} | recoverable from same PO elsewhere in sheet: {rec}')
for r in ev[:6]: print('  ', r[0], '|', r[3][:24], '|', r[9][:34])

print()
print('== TT: vendor-number col sanity ==')
tt = rows['Tanda Terima']
vn = collections.Counter(re.sub(r'\D', '', r[3])[:3] if len(r) > 3 else '' for r in tt[1:])
print('vendor-number prefixes:', vn.most_common(6))
