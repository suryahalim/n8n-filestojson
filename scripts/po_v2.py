"""po_v2.py — PO Customer tab, 13-column rules schema (2026-10-01).

Columns: 0 PO No | 1 Vendor Code (SAMB's code in client's eyes) | 2 PO Issuer (customer of SAMB)
         3 PPN ('11%'|'1.1%') | 4 Product Code | 5 Product Name | 6 Qty | 7 UON
         8 Unit Price | 9 Discount | 10 Total | 11 Source Page | 12 Mapping Status

User rules (recorded in knowledge/po_rules.json):
  * PPN is only 11% or 1.1%.
  * 'VENDOR : 300045730' = vendor CODE (SAMB code at the client), never a name.
  * PO Issuer = the customer (always PT-prefixed when legal name is printed; store name allowed + tagged).
  * Total = Qty x Unit Price - Discount (PPN-incl variant allowed when the tab prints gross).
  * SAMB (and its OCR-mangled variants) is never issuer and never vendor.
Never invents: unknown fields stay blank; failed arithmetic -> status REVIEW-ARITH.
"""
import re, json, os

from map_sheets import g, num as N, classify

HERE = os.path.dirname(__file__)
SELF = re.compile(r'SARANA|SAHANA|S?ABADI\s+MAKMUR|KIRANA ABADI|MAJU ABADI|'
                  r'MA KMUR|BLKSAMA|BERSAMA[.,\s]*PT', re.I)
JUNK_ISSUER = re.compile(r'^(?:PT\.?\s*)?(ADDRESS|TOTAL|NAMA|NAME|NO|JALAN|JL|ORDER|DAN|YANG|TOKO|ALAMAT|VENDOR|KODE|SUPPLIER|SEND|SHIP|DELIVERY)\b', re.I)
JUNK_SUFFIX = re.compile(r'\s+(SUDAH|PLEASE|SEND|TO|FROM|FOR|WITH)\b.*$', re.I)
PT_RE = re.compile(r'\bPT\.?\s*[A-Z][A-Za-z .,&\'()\-]{4,48}?(?:TBK|\(TBK\))?(?=\s*(?:$|[,:(\n])|\s{2,})', re.M)
VCODE_RE = re.compile(r'(?:VENDOR|No\.?\s*Supplier|Kode\s*Supplier|Supplier(?:\s*No)?(?:\s*/\s*Contract\s*No)?|Vendor\s*Code)\s*[:\s]*\n?\s*\(?(V?-?\d{5,14}|[A-Z]{2,5}\d(?:\.[A-Z0-9.]+){2,6})\)?', re.I)

def _load_issuers():
    try:
        return json.load(open(os.path.join(HERE, '..', 'knowledge', 'issuers.json')))
    except Exception:
        return {}

ISSUERS = _load_issuers()

MONEY = r'\d{1,3}(?:[.\s]\d{3})*(?:,\d+)?|\d+(?:[.,]\d+)?'

def money(v):
    """Parse Indonesian money as printed. Returns float or None."""
    if v is None:
        return None
    s = str(v).strip().replace('%', '').replace('Rp.?', '', 1).replace('IDR', '').strip()
    if not s or s in ('-', '--', '0.00'.replace('0.00', 'x')) and s != '0':
        return None
    if re.fullmatch(r'\d{1,3}(?:\.\d{3})+(?:,\d+)?', s):
        return float(s.replace('.', '').replace(',', '.'))
    if re.fullmatch(r'\d{1,3}(?:,\d{3})+(?:\.\d+)?', s):
        return float(s.replace(',', ''))
    if re.fullmatch(r'\d{1,3}(?:\.\d{3})*,\d{1,2}', s):
        return float(s.replace('.', '').replace(',', '.'))
    if re.fullmatch(r'\d{1,3}(?:,\d{3})*\.\d{1,2}', s):
        return float(s.replace(',', ''))
    if re.fullmatch(r'\d+(?:[.,]\d+)?', s):
        return float(s.replace(',', '.'))
    return None

def ppn_of(t):
    """PPN per rules: 11% or 1.1% only. Printed percents/DPP-ratio win; else blank."""
    u = t.upper()
    m = re.search(r'(?:PPN|PPnBM|VAT)\s*(?:%|RATE|TARIF)?\s*[:\s]*(' + MONEY + r')\s*%', t)
    if not m:
        m = re.search(r'(?:PPN|VAT)\s*%?\s*[:\s]*\n?\s*(' + MONEY + r')(?:\s*\n|$)', t)
    if m:
        v = money(m.group(1))
        if v is not None and 0 < v <= 100:
            return '11%' if v >= 8 else ('1.1%' if v < 4 else '11%')
    md = re.search(r'(?:DPP|DASAR\s+PENGENAAN\s+PAJAK)\s*[:\s]*(' + MONEY + r')', t, re.I)
    mp = re.search(r'(?:PPN|Ppn|PPnBM)\s*[:\s]*(' + MONEY + r')\s*(?:\n|TOTAL|GRAND|SUB)', t)
    if md and mp:
        d, p = money(md.group(1)), money(mp.group(1))
        if d and p and d > 0:
            r = p / d
            if 0.08 < r < 0.14:
                return '11%'
            if 0.005 < r < 0.02:
                return '1.1%'
    return ''

def vendor_code_of(t):
    m = VCODE_RE.search(t)
    if not m:
        return ''
    c = m.group(1).strip().strip('().')
    return c if len(c) >= 5 else ''

def _issuer_ok(s):
    s = str(s or '').strip()
    if not s or s.isdigit():
        return False
    if SELF.search(s):
        return False
    return bool(re.search(r'[A-Z]{4,}', s.upper()))

def issuer_of(t, fallback_name=''):
    """PT legal name on the page (non-self, non-junk), else brand alias proven in issuers.json,
    else printed store name tagged |ISSUER-STORENAME."""
    cands = []
    for m in PT_RE.finditer(t):
        s = re.sub(r'\s+', ' ', m.group(0)).strip().rstrip('.,')
        s = JUNK_SUFFIX.sub('', s)
        if SELF.search(s) or JUNK_ISSUER.search(s.replace('PT. ', 'PT').replace('PT ', 'PT')):
            continue
        if len(s) < 9:
            continue
        cands.append(s.upper())
    for brand, ent in ISSUERS.items():
        pt = ent.get('pt') or ''
        if pt and re.search(ent.get('re', brand), t.upper()):
            for c in cands:
                if re.sub(r'[^A-Z]', '', pt)[:10] in re.sub(r'[^A-Z]', '', c):
                    return _issuer_ok(pt) and pt or '', ''
            if not cands:
                return _issuer_ok(pt) and pt or '', '|ISSUER-BRAND'
    pt_cands = [c for c in cands if c.startswith(('PT', 'CV'))]
    if pt_cands:
        return pt_cands[0], '|ISSUER-PT-PAGE'
    if cands:
        return cands[0], '|ISSUER-PT-PAGE'
    if fallback_name and _issuer_ok(fallback_name):
        return fallback_name.strip(), '|ISSUER-STORENAME'
    return '', '|ISSUER-MISSING'

def gate(q, price, disc, total, ppn):
    """Rules gate: Total = Q*P - D (or PPN-incl variant). Returns tag or ''."""
    if None in (q, price, total):
        return '|VERIFY'
    d = disc or 0.0
    net = q * price - d
    ok = abs(net - total) / max(total, 1.0) < 0.02
    if not ok and ppn:
        f = 1.11 if ppn == '11%' else 1.011
        ok = abs(net * f - total) / max(total, 1.0) < 0.02
    return '' if ok else '|REVIEW-ARITH'

def split_prod(s):
    s = str(s or '').strip()
    m = re.match(r'^(\d{6,14})\s+(.+)$', s, re.S)
    if m:
        return m.group(1), re.sub(r'\s+', ' ', m.group(2)).strip()
    if re.fullmatch(r'\d{6,14}', s):
        return s, ''
    return '', s

def row(po, vcode, issuer, ppn, prod, qty, uon, price, disc, total, page, status='OCR_ITEM', i_tag=''):
    code, name = split_prod(prod)
    q = N(str(qty)) if isinstance(qty, str) else qty
    pr = N(str(price)) if isinstance(price, str) else price
    dc = N(str(disc)) if isinstance(disc, str) else disc
    tt = N(str(total)) if isinstance(total, str) else total
    tag = gate(q, pr, dc, tt, ppn)
    return [po or '', vcode or '', issuer or '', ppn or '', code, name, q, uon or '', pr, dc, tt,
            page, (status or 'OCR_ITEM') + tag + (i_tag or '')]

# ------------------------------ layout parsers -------------------------------
def po_hypermart_epo(t, page):
    if not (re.search(r'PO#\s*:', t) and re.search(r'E-PO|Hypermart|Hypermat', t, re.I)):
        return []
    po = g(t, r'PO#\s*:\s*([0-9A-Z][0-9A-Z./-]{3,})')
    vc = vendor_code_of(t)
    iss, itag = issuer_of(t, next((l for l in map(str.strip, t.splitlines())
                                    if re.search(r'Hypermart\s+\w', l, re.I)), ''))
    ppn = ppn_of(t)
    rows = []
    for l in t.splitlines():
        m = re.match(r'^\s*(\d{1,5})\s+(\S.*?)\s+(\d{10,14})\s+(\d{6,10})\s+(\d+)\s+(\d+)\s+(' +
                     MONEY + r')\s+(' + MONEY + r')(?:\s+(' + MONEY + r'))?(?:\s+(' + MONEY + r'))?', l)
        if not m:
            continue
        q, name, upc, sku, c1, c2, price, v1, v2, v3 = m.groups()
        qv, pr = float(q), money(price)
        vals = [money(x) for x in (v1, v2, v3) if x]
        vals = [v for v in vals if v is not None]
        if pr is None or not vals:
            continue
        tt = max(vals)
        disc = 0.0
        for v in vals:
            if v != tt and abs(qv * pr - v - tt) / max(tt, 1.0) < 0.03:
                disc = v
        rows.append(row(po, vc, iss, ppn, sku + ' ' + name, qv, '', pr, disc, tt, page))
    return rows

def po_lion(t, page):
    if not re.search(r'LION SUPER INDO|SUPER INDO', t.upper()) and 'PURCHASE ORDER' not in t.upper():
        return []
    po = g(t, r'(?:PO Number|NO PO)\s*:?\s*([A-Z][A-Z0-9./-]{5,})')
    if not po:
        return []
    vc = vendor_code_of(t)
    iss, itag = issuer_of(t, 'PT. LION SUPER INDO' if re.search(r'LION SUPER INDO', t.upper()) else '')
    ppn = ''
    rows = []
    for l in t.splitlines():
        m = re.match(r'^\s*\d+\.\s+(\d{6,12})\s+(.+?)\s+(\d+)\s+-\s+(' + MONEY + r')\s+(' +
                     MONEY + r')\s+(' + MONEY + r')\s+(' + MONEY + r')\s*$', l)
        if not m:
            continue
        code, name, q, price, disc, ppnv, total = m.groups()
        d = money(ppnv)
        p_ = '11%' if (d or 0) > 50 else ''
        rows.append(row(po, vc, iss, ppn or p_, code + ' ' + name, float(q), '', money(price),
                        money(disc), money(total), page))
    return rows

def po_astro(t, page):
    if 'PURCHASE ORDER DETAILS' not in t.upper():
        return []
    po = g(t, r'PO Number\s*\n?\s*([A-Z0-9/][A-Z0-9/-]{5,})')
    vc = vendor_code_of(t)
    iss, itag = issuer_of(t, 'PT Astro Technologies Indonesia' if re.search(r'ASTRO', t.upper()) else '')
    ppn = ppn_of(t)
    rows = []
    for l in t.splitlines():
        m = re.match(r'^([A-Z0-9][A-Z0-9./-]{5,20})\s+(.+?)\s+(\d+)\s+(' + MONEY + r')\s+(' +
                     MONEY + r')$', l)
        if not m or m.group(1) == po:
            continue
        sku, name, q, price, total = m.groups()
        rows.append(row(po, vc, iss, ppn, sku + ' ' + name, float(q), '', money(price), 0.0,
                        money(total), page))
    return rows

def po_midi_lpb(t, page):
    """Midi/Midi & Alfa 'LAPORAN/PENERIMAAN BARANG' — PO No + supplier code + PLU items."""
    if not re.search(r'PENERIMAAN BARANG|LAPORAN PENERIMAAN', t.upper()):
        return []
    po = g(t, r'No\s*\.?\s*PO\s*:\s*([0-9A-Z./-]{6,})')
    if not po:
        return []
    mvc = re.search(r'Kode\s*Supplier\s*:\s*([A-Z0-9][A-Z0-9.]{5,20})', t)
    vc = mvc.group(1) if mvc else vendor_code_of(t)
    iss, itag = issuer_of(t, 'PT. MIDI UTAMA INDONESIA TBK' if re.search(r'MIDI', t.upper()) else '')
    ppn = ppn_of(t)
    rows = []
    for l in t.splitlines():
        m = re.match(r'^\s*(?:#?\s*\d+\.?\s*\|?\s*)?(\d{6,8})\s*\|?\s+(.+?)\s*\|?\s+([\d.,]+)\s*(?:PC|PCS)?\s*\|?\s*' +
                     r'(\d+\s*\*\s*\d+|\d+)\s*\|?\s+(' + MONEY + r')\s*\|?\s+(.+?)\s*\|?\s+(?:0|\d)\s*\|?\s+(' +
                     MONEY + r')', l)
        if not m:
            continue
        plu, name, qty, isi, price, pot, total = m.groups()
        q = money(qty)
        if q is None:
            continue
        pr = money(price)
        rows.append(row(po, vc, iss, ppn, plu + ' ' + name, q, '', pr, 0.0, money(total), page))
    return rows

def po_aeon_slip321(t, page):
    """AEON slip321: vertical key/value header, items 'n / DESC-lines / 1.00 / EACH / itemno / barcode / qty / disc / - / price / amount'."""
    u = t.upper()
    if 'AEON' not in u or 'PO No' not in t:
        return []
    po = g(t, r'PO No\s*\n?\s*(\d{12,16})')
    vc = g(t, r'Supplier No / Contract No\s*\n?\s*([0-9A-Z/-]{6,20})')
    vc = re.sub(r'\s*/.*', '', vc).strip() if vc else vendor_code_of(t)
    iss, itag = issuer_of(t, 'PT AEON INDONESIA')
    ppn = ppn_of(t)
    rows = []
    lines = [l.strip() for l in t.splitlines() if l.strip()]
    for i, l in enumerate(lines):
        if not re.fullmatch(r'\d{1,3}', l) or i + 8 >= len(lines):
            continue
        # find EACH marker within next 5 lines
        j = next((k for k in range(i + 1, min(i + 6, len(lines)))
                  if lines[k].upper() in ('EACH', 'CTN', 'CS')), None)
        if j is None:
            continue
        desc = ' '.join(lines[i + 1:j])
        tail = lines[j:j + 10]
        nums = [money(x) for x in tail if re.fullmatch(MONEY, x)]
        itemno = next((x for x in tail if re.fullmatch(r'\d{8}', x)), '')
        bc = next((x for x in tail if re.fullmatch(r'\d{12,14}', x)), '')
        big = [v for v in nums if v and v > 1000]
        if len(big) >= 2:
            qv, pr, tt = None, None, None
            try:
                oi = tail.index(itemno) if itemno in tail else 0
            except ValueError:
                oi = 0
            after = [x for x in tail[oi:] if re.fullmatch(MONEY, x)]
            vals = [money(x) for x in after]
            vals = [v for v in vals if v is not None]
            qv = next((v for v in vals if 0 < v <= 5000), None)
            pr = next((v for v in vals if v >= 100), None)
            tt = max(vals) if vals else None
            if qv and pr and tt and abs(qv * pr - tt) / max(tt, 1) < 0.03:
                rows.append(row(po, vc, iss, ppn, (bc or itemno) + ' ' + desc, qv, lines[j], pr, 0.0, tt, page))
    return rows

def po_grandlucky(t, page):
    if not re.search(r'GRAND ?LUCKY', t.upper()) and not re.search(r'ORDER TO', t.upper()):
        return []
    po = g(t, r'PO#\s*:\s*([0-9][0-9A-Z.]{5,})')
    if not po:
        return []
    vc = vendor_code_of(t)
    iss, itag = issuer_of(t, 'GrandLucky Superstore')
    ppn = ppn_of(t)
    rows = []
    for l in t.splitlines():
        m = re.match(r'^\s*(\d{1,3})\s+(\d{9,12})\s+(.+?)\s+([A-Z]{1,5}/\d+|CRT/\d+|CTN/\d+)\s+(\d{12,14})\s+(\d+)\s+([A-Za-z]{2,5})\s+(' +
                     MONEY + r')\s+(' + MONEY + r')\s*$', l)
        if not m:
            continue
        idx, sku, name, pack, bc, q, uom, price, total = m.groups()
        rows.append(row(po, vc, iss, ppn, sku + ' ' + name + ' ' + pack, float(q), uom, money(price),
                        0.0, money(total), page))
    return rows

def po_harihari(t, page):
    if not re.search(r'HARI HARI|PASAR SWALAYAN', t.upper()):
        return []
    po = g(t, r'No PO\s*:\s*([0-9A-Z./-]{5,})')
    if not po:
        return []
    vc = g(t, r'No Supplier\s*:\s*(\d{5,12})') or vendor_code_of(t)
    iss, itag = issuer_of(t, 'Hari Hari Pasar Swalayan')
    ppn = ppn_of(t)
    rows = []
    lines = [l.strip() for l in t.splitlines() if l.strip()]
    for i, l in enumerate(lines):
        if not re.fullmatch(r'\d{1,3}', l):
            continue
        win = lines[i + 1:i + 9]
        sku = next((x for x in win[:2] if re.fullmatch(r'\d{7,9}', x)), '')
        bc = next((x for x in win[:3] if re.fullmatch(r'\d{12,14}', x)), '')
        nm = next((x for x in win[:3] if re.search(r'[A-Za-z]{4,}', x)), '')
        hp = next((x for x in win[:5] if re.fullmatch(r'\d{1,3}[.,]\d{3}', x)), '')
        cq_pos = next((j for j, x in enumerate(win) if re.fullmatch(r'\d{1,4} ?(PC|PCS|CTN|BOX)', x, re.I)), None)
        if not (sku and nm and hp and cq_pos is not None):
            continue
        mm = re.fullmatch(r'(\d{1,4}) ?(PC|PCS|CTN|BOX)', win[cq_pos], re.I)
        i_case = int(mm.group(1))
        nxt = next((x for x in win[cq_pos + 1:cq_pos + 3] if re.fullmatch(r'\d{1,5}', x)), '')
        if not nxt:
            continue
        qv = float(nxt)
        rows.append(row(po, vc, iss, ppn, (bc or sku) + ' ' + nm, qv * i_case, 'PCS',
                        money(hp), 0.0, qv * i_case * money(hp), page))
    return rows

def po_dutabuah(t, page):
    if not re.search(r'DUTA BUAH', t.upper()):
        return []
    po = g(t, r'NOMER ORDER\s+([A-Z0-9./-]{6,})')
    vc = vendor_code_of(t)
    iss, itag = issuer_of(t, 'Duta Buah Segar')
    ppn = ppn_of(t)
    rows = []
    for l in t.splitlines():
        m = re.match(r'^(\d{12,14})\s+(.+?)\s+(\d+)\s+(PCS|CTN|BOX)\s+(' + MONEY + r')\s+\d*%?\|?[\d%|Rp.]*\s+\d+\s+(' + MONEY + r')$', l)
        if not m:
            continue
        bc, name, q, uom, price, total = m.groups()
        rows.append(row(po, vc, iss, ppn, bc + ' ' + name, float(q), uom, money(price), 0.0, money(total), page))
    return rows

def po_gramedia(t, page):
    if not re.search(r'GRAMEDIA|ASRI MEDIA', t.upper()):
        return []
    po = g(t, r'Purchase Order No\s*:\s*([A-Z0-9-]{8,})') or g(t, r'\n([A-Z]{3}\d{6,}-\d{6,})\n')
    mvc = re.search(r'Supplier\s*:\s*(V?0*[-\s]?[A-Z]?-?\d{5,12})', t, re.I)
    vc = re.sub(r'^V0*-+', '', (mvc.group(1) if mvc else vendor_code_of(t))).strip()
    iss, itag = issuer_of(t, 'PT Gramedia Asri Media' if re.search(r'GRAMEDIA', t.upper()) else '')
    ppn = ppn_of(t)
    rows = []
    for l in t.splitlines():
        m = re.match(r'^\s*\d+\s+(\d{6,12})\s+(\d{8,14})\s+(.+?)\s+(\d+)\s+(' + MONEY + r')\s+(' + MONEY +
                     r')\s+(' + MONEY + r')\s+(' + MONEY + r')\s*$', l)
        if not m:
            continue
        itemno, ean, name, q, price, d1, d2, amount = m.groups()
        rows.append(row(po, vc, iss, ppn, itemno + ' ' + name, float(q), '', money(price), 0.0,
                        money(amount), page))
    return rows

def po_kage(t, page):
    if not re.search(r'KAGE DWIJAYA', t.upper()):
        return []
    po = g(t, r'No\.?\s*:\s*([A-Z]{2}-\d{5,})')
    vc = vendor_code_of(t)
    iss, itag = issuer_of(t, 'PT. KAGE DWIJAYA')
    ppn = ppn_of(t)
    rows = []
    for l in t.splitlines():
        m = re.match(r'^(\d+)\s+(.+?)\s+(\d{6,10})\s+(\d+)\s+(' + MONEY + r')\s+(?:[\d.,]+\s+){0,3}(' + MONEY + r')$', l)
        if not m:
            continue
        idx, name, code, q, price, total = m.groups()
        rows.append(row(po, vc, iss, ppn, code + ' ' + name, float(q), '', money(price), 0.0, money(total), page))
    return rows

def po_berkah(t, page):
    if not re.search(r'PT BERKAH', t.upper()):
        return []
    po = g(t, r'PO#?\s*:\s*(POID[A-Z0-9]{6,})')
    vc = vendor_code_of(t)
    m = re.search(r'(PT BERKAH [A-Z]+ OPS)', t.upper())
    iss, itag = issuer_of(t, m.group(1) if m else 'Berkah Operations')
    ppn = ppn_of(t)
    rows = []
    for l in t.splitlines():
        m = re.match(r'^(ID\d{4,8})\s+(\d{6,14})\s+(.+?)\s+(\d+)/[A-Z0-9]+\s+CTN\s+(' + MONEY + r')\s+([\d.]+)%\s+(' +
                     MONEY + r')\s+\S+\s+\S*\s*(?:-)?\s*(' + MONEY + r')\s+(\d+)\s+(' + MONEY + r')\s+(' + MONEY + r')', l)
        if not m:
            continue
        san, bc, name, pallets, gross, td, tdamt, net, qty, netamt, vat = m.groups()[:10]
        rows.append(row(po, vc, iss, ppn or '11%', san + ' ' + name, float(qty), 'CTN', money(gross),
                        money(td), money(netamt), page))
    return rows

def po_lafonte_scan(t, page):
    """Scanned 'Toko' PO booklets (code|NAME|isi|harga|hpp pcs|order...) — OCR noisy; header-only fallback."""
    if not re.search(r'1100000382|11 ?0000 ?0010|NAMA TOKO|LAFONTE', t.upper()):
        return []
    po = ''
    iss, itag = issuer_of(t, '')
    return []  # items unreliable from OCR; header fallback via generic below

# old-parser adapter -----------------------------------------------------------
def adapt_old(r11, t, page):
    po, vendor, ppn, prod, qty, uon, price, disc, total, pg, status = r11[:11]
    iss, itag = issuer_of(t)
    # old vendor col held a company name = the customer/issuer under new rules
    if vendor and not SELF.search(str(vendor)) and not iss:
        iss, itag = str(vendor).strip(), '|ISSUER-PAGE'
    vcode = vendor_code_of(t)
    return row(po, vcode, iss, ppn_of(t) if not ppn else ppn, prod, qty, uon, price, disc, total, pg, status, itag)

HEADER_ONLY_RE = re.compile(r'PURCHASE ORDER', re.I)
def po_generic_header(t, page):
    """Last-resort: PO no + issuer + vendor code + PPN, no items (status PO-HEADER-NOITEMS)."""
    po = (g(t, r'PO#\s*:\s*([0-9][0-9A-Z./-]{3,})') or g(t, r'(?:No\.?\s*PO|NO PO|PO Number|Nomor P?\.?O|Purchase Order No\.?)\s*[:\s]\s*([0-9A-Z][0-9A-Z./_-]{4,})')
          or g(t, r'P\.O No\s*[:\s]\s*([0-9A-Z][0-9A-Z./-]{4,})') or g(t, r'ORDER NO\s*\n\s*([A-Z0-9]{6,})'))
    if not po or not HEADER_ONLY_RE.search(t):
        return []
    vc = vendor_code_of(t)
    iss, itag = issuer_of(t, '')
    if not iss:
        m = re.search(r'(?:PO Issued By|WP\s*:)\s*[:\s]*\n?\s*(PT[^\n]{5,50})', t, re.I)
        if m:
            iss, itag = m.group(1).strip(), '|ISSUER-WP'
    if not iss:
        m = re.search(r'^(?:Dikirim Dari|Store|OUTLET)\s*:\s*([^\n]{5,40})', t, re.I | re.M)
        if m and not SELF.search(m.group(1)):
            iss, itag = m.group(1).strip(), '|ISSUER-STORENAME'
    ppn = ppn_of(t)
    return [[po, vc, iss, ppn, '', '', '', '', '', '', '', page, 'PO-HEADER-NOITEMS' + (itag or '')]]

CHAIN = [po_hypermart_epo, po_lion, po_astro, po_midi_lpb, po_aeon_slip321, po_grandlucky,
         po_harihari, po_dutabuah, po_gramedia, po_kage, po_berkah]

def build_page(t, page):
    """Returns 13-col rows for a PO-classified page, or [] if not a PO page at all."""
    out = []
    for f in CHAIN:
        try:
            out = f(t, page)
        except Exception:
            out = []
        if out:
            return out
    # fallback: header + generic arithmetic-gated item tokenizer (user rule 2026-10-01:
    # rows must carry Product Name; header-only is the LAST resort, not the norm)
    try:
        out = po_fallback_items(t, page)
    except Exception:
        out = []
    if out:
        return out
    if 'PURCHASE ORDER' in t.upper():
        try:
            out = po_generic_header(t, page)
        except Exception:
            out = []
        if out:
            return out
    return out

def po_fallback_items(t, page):
    """Last parser chain stage: PO header (same extraction as generic) + itemize() of the
    page text. Every accepted row passes qty*price ~= total, so no invented values."""
    import item_fallback
    items = item_fallback.itemize(t)
    if not items:
        return []
    po = (g(t, r'PO#\s*:\s*([0-9][0-9A-Z./-]{3,})') or g(t, r'(?:No\.?\s*PO|NO PO|PO Number|Nomor P?\.?O|Purchase Order No\.?)\s*[:\s]\s*([0-9A-Z][0-9A-Z./_-]{4,})')
          or g(t, r'NOMER ORDER\s+(PO\.?[0-9][0-9./A-Z-]{4,})') or g(t, r'NOMER ORDER\s+([0-9A-Z][0-9A-Z./_-]{4,})')
          or g(t, r'P\.O No\s*[:\s]\s*([0-9A-Z][0-9A-Z./-]{4,})') or g(t, r'ORDER NO\s*\n\s*([A-Z0-9]{6,})'))
    vc = vendor_code_of(t)
    iss, itag = issuer_of(t, '')
    if not iss:
        m = re.search(r'^([A-Z][A-Z .&\'()\-]{6,55})$', t, re.M)   # ALLCAPS header line = store/legal name
        if m and not SELF.search(m.group(1)) and not JUNK_ISSUER.search(m.group(1)):
            iss, itag = m.group(1).strip(), '|ISSUER-STORENAME'
    if not iss:
        m = re.search(r'DIKIRIM UNTUK[^\n:]*:\s*\n\s*([A-Z][^\n]{2,40})|Kirim Ke\s*[:\s]\s*([^\n]{3,40})|SHIP TO\s*[:\s]\s*([^\n]{3,40})', t, re.I)
        cand = next((x for x in (m.groups() if m else []) if x), '')
        if cand and not SELF.search(cand):
            iss, itag = cand.strip(), '|ISSUER-SHIP'
    ppn = ppn_of(t)
    rows = []
    for code, name, qty, uon, price, disc, total, derived in items:
        st = 'MAPPED|ITEM-FALLBACK' if not derived else 'MAPPED|ITEM-FALLBACK|PRICE-DERIVED'
        if itag:
            st += itag
        rows.append([po or '', vc, iss, ppn, code, name, qty, uon, price, disc, total, page, st])
    return rows
