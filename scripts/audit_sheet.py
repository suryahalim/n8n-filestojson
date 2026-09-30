import json, re, collections

rows = json.load(open('/tmp/sheet_now.json'))

def isnum(s):
    s = str(s).strip().replace(' ', '')
    return bool(re.fullmatch(r'-?\d{1,3}(\.\d{3})*(,\d+)?|-?\d+(,\d+)?', s))

print('=' * 25, 'Faktur Penjualan', '=' * 25)
fp = rows['Faktur Penjualan']
h = fp[0]
print('headers:', h)
c_stat = collections.Counter(r[17] if len(r) > 17 else '' for r in fp[1:])
print('status:', dict(c_stat))
# vendor column distribution
c_ven = collections.Counter(r[1] for r in fp[1:] if len(r) > 1)
print('top vendors:', c_ven.most_common(8))
# date sanity
bad_date = [r for r in fp[1:] if r[4] and not re.search(r'\d', str(r[4]))]
print('rows with non-date col4:', len(bad_date))
# qty/price that are non-numeric garbage
bad_qty = [r for r in fp[1:] if r[5] != '' and not isnum(r[5])]
print('qty non-numeric:', len(bad_qty), [r[5] for r in bad_qty[:6]])
# sum check per SOR
def n2(s):
    s = str(s).strip()
    if not s or not isnum(s): return None
    try:
        if re.fullmatch(r'\d{1,3}(\.\d{3})+(,\d+)?', s): s = s.replace('.', '').replace(',', '.')
        elif ',' in s: s = s.replace(',', '.')
        return float(s)
    except Exception: return None
per = collections.defaultdict(list)
for r in fp[1:]:
    if len(r) > 17 and r[17] != 'SUMMARY':
        per[(r[3], r[0])].append((n2(r[7]), n2(r[4]) if False else n2(r[7]), r))
# footer mismatch rows
c_fp = [r for r in fp[1:] if len(r) > 17 and 'FP' in str(r[16]) and '✓' not in str(r[16]) and r[16] != '']
print('FP cross-check not-pass:', len(c_fp))

print('=' * 25, 'PO Customer', '=' * 25)
po = rows['PO Customer']
print('headers:', po[0])
c_ven = collections.Counter(r[1] for r in po[1:] if len(r) > 1)
print('vendors:', c_ven.most_common(12))
c_st = collections.Counter((r[10] if len(r) > 10 else '') for r in po[1:])
print('status col10:', c_st.most_common(8))

print('=' * 25, 'Tanda Terima', '=' * 25)
tt = rows['Tanda Terima']
print('headers:', tt[0])
empt = [i for i, r in enumerate(tt[1:], 2) if not any(str(c).strip() for c in r[:-1])]
print('rows with all-empty data cols:', len(empt))
c_q = collections.Counter((r[6] if len(r) > 6 else '') for r in tt[1:])
print('status col6:', dict(c_q))

print('=' * 25, 'Surat Jalan', '=' * 25)
sj = rows['Surat Jalan']
print('headers:', sj[0])
for r in sj[1:6]: print(' ', r)

print('=' * 25, 'Faktur Pajak', '=' * 25)
fpj = rows['Faktur Pajak']
print('headers:', fpj[0])
no_seri = [r for r in fpj[1:] if not r[2]]
print('rows missing Kode Seri:', len(no_seri))
c_npwp = collections.Counter(len(r[5].replace('.', '')) if len(r) > 5 else 0 for r in fpj[1:])
print('NPWP-pembeli length dist:', dict(c_npwp))

print('=' * 25, 'sample suspicious FP rows', '=' * 25)
import random
random.seed(7)
susp = [r for r in fp[1:] if r[17] in ('PASS', 'MISMATCH') or '✓' not in r[16]]
for r in random.sample(fp[1:], 12):
    print(' |', r[1][:22], '|', r[2][:14], '|', r[4], '|', r[5], r[6], r[7], r[8], '|', r[17], '|', r[16][:20])
