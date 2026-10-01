"""item_fallback.py — generic PO item-line tokenizer (user rule 2026-10-01:
'an item row must carry a Product Name; empty REVIEW rows are not acceptable').

Runs on PO pages where the per-vendor parser chain produced ZERO items. A line is an
item if it has a real name (>=3 letters) plus >=2 numerics; qty*price must satisfy the
printed total within tolerance (arithmetic gate) or the line is skipped — we never
invent. Spec-pairs like '10 GR' are greedily absorbed into the product name while
qty/price/total arithmetic still closes on the remainder."""
import re

_HDRROW = re.compile(r'^\s*(?:N[OA]\.?\s+|SKU\s|ARTIKEL|KODE|ITEM|BRG\b|SEQ\b|TGL\b|TANGGAL\b|'
                     r'TOTAL\b|GRAND\b|SUBTOTAL\b|PPN\b|TAX\b|DISKON\b|HARGA\b|QTY\b|JML\b|'
                     r'U/?O/?N\b|SATUAN\b|KETERANGAN|KETER\b|REMARK|CATATAN|HAL\b|PAGE\b|ATTN|'
                     r'FAX|TEL|TSLP|TELP|TO\b|FROM\b|DIBUAT\b|HORMAT\b|TERIMAKASIH\b|'
                     r'TANDA TERIMA\b|PURCHASE ORDER|ORDER NO|DELIVERY|GUDANG|ALAMAT|JL\.|JALAN\b)', re.I)
_UNIT = re.compile(r"^(?:GR|GRAM|G|ML|L|LTR|LT|KG|PCS|PC|BOX|CTN|CRT|KRG|BLK|LNN|LST|LSN|SACH|"
                   r"SACHET|DZN|ROLL|RLL|PAC|EA|BAL|CT|UNIT|BOTOL|BTL|TIN|CAN|PAKET|PCK|STRIP|"
                   r"BTG|KOTAK|KOT|KRAT|KRT|DUS|CARDS|CAR|CM|SETS?|SHEETS?|CARTRIDGES?)$", re.I)
_PCT = re.compile(r'^\d*(?:[.,]\d+)?%')

def _num(t):
    t = t.strip()
    if re.fullmatch(r'\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?', t):
        return float(t.replace('.', '').replace(',', '.'))
    if re.fullmatch(r'\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?', t):
        return float(t.replace(',', ''))
    if re.fullmatch(r'\d{1,3}(?:\.\d{3})+', t):
        return float(t.replace('.', ''))
    if re.fullmatch(r'\d{1,3}(?:,\d{3})+', t):
        return float(t.replace(',', ''))
    if re.fullmatch(r'\d+(?:[.,]\d{1,2})?', t):
        return float(t.replace(',', '.'))
    return None

def parse_line(line):
    """-> [code, name, qty, uon, price, discount, total, derived] or None (arithmetic-gated).

    derived=True means unit price was not printed standalone (inferred as total/qty)."""
    line = line.strip()
    if len(line) < 8 or _HDRROW.match(line):
        return None
    toks = line.split()
    code = ''
    if re.fullmatch(r'\d{9,14}', toks[0]):
        code, toks = toks[0], toks[1:]
    i, name = 0, []
    while i < len(toks) and not re.search(r'\d', toks[i]) and not _PCT.match(toks[i]):
        name.append(toks[i]); i += 1
    best = None
    for absorb in range(6, -1, -1):   # prefer RICHER name: absorb up to 6 spec pairs
        j = i
        nm2 = list(name)
        ok = True
        for k in range(absorb):
            if j + 1 < len(toks) and re.fullmatch(r'(?:\d{1,4}|\d{1,3}[Xx]\d{1,3})', toks[j]) and _UNIT.match(toks[j + 1]):
                nm2.append(toks[j]); nm2.append(toks[j + 1]); j += 2
            else:
                ok = False
                break
        if not ok:
            continue
        rest = toks[j:]
        moneys = []
        for tk in rest:
            if '|' in tk or _PCT.match(tk):
                continue
            v = _num(tk)
            if v is not None:
                moneys.append((tk, v))
        vals = [v for _, v in moneys if v > 0]
        if len(vals) < 2:
            continue
        total = vals[-1]
        tol = max(0.02 * total, 5.0)
        cands = []
        for qi in range(min(len(vals) - 1, 4)):
            q = vals[qi]
            if q <= 0 or q > 20000 or q != int(q):
                continue
            q = int(q)
            u2 = ''
            for mi, (tk, v) in enumerate(moneys):
                if v == q:
                    pos = [p for p, x in enumerate(rest) if x == tk]
                    if pos:
                        nxt = rest[pos[0] + 1:pos[0] + 2]
                        if nxt and _UNIT.match(nxt[0]):
                            u2 = nxt[0].upper()
                    break
            for pj in range(qi + 1, len(vals) - 1):
                if abs(q * vals[pj] - total) <= tol:
                    cands.append([code, ' '.join(nm2).strip(), q, u2, vals[pj], 0, total, False])
                    break
            if cands:
                break
            if len(vals) == 2 and q and abs(total / q - vals[1]) < 0.011:
                cands.append([code, ' '.join(nm2).strip(), q, u2, round(total / q, 2), 0, total, True])
                break
        if cands:
            best = cands[0]
            break
    if best and len(re.sub(r'[^A-Za-z]', '', best[1])) < 3:
        return None
    return best

def itemize(text):
    out, seen = [], set()
    for line in (text or '').splitlines():
        r = parse_line(line)
        if r:
            k = (r[0], r[1], r[2], round(r[6]))
            if k not in seen:
                seen.add(k)
                out.append(r)
    return out

if __name__ == '__main__':
    tests = [
        '8990800021362 ALPENLIEBE LOZY KARAMEL 10 GR 48 PCS 683 0%|0%|0%|Rp.0 0 32,760.00',
        '8935001730361 CHUPA CHUPS CRAZY RAFFE 30 GR 48 PCS 5,994 0%|0%|0%|Rp.0 0 287,697.30',
        '8991115012106 BIGBABOL STRAWBERRY STICK 20G 40 PCS 2,061 0%|0%|0%|Rp.0 0 82,457.66',
        '11114801000801 TUC RITZ 260 GR 6 BOX 39,000 0%|0%|0%|Rp.0 0 234,000.00',
        '00000502588626 BOLA-OLA KOTAK 6X200 GRAM 12 BOTOL 34,000 32,000 384,000.00',
        'TOTAL QTY 286', 'TOTAL HARGA 698,556', 'TAX 76,841', 'GROCERY CONFECTIONARY',
        'BSD', 'PT SARANA ABADI MAKMUR BERSAMA',
    ]
    for ln in tests:
        print('->', parse_line(ln), '  |', ln[:48])
