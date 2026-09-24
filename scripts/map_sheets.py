#!/usr/bin/env python3
"""Map extractor OCR JSON -> the six 'Result RPA' Google Sheet tabs.

Deterministic, per-vendor-family parsers. NEVER invents values: a field that
cannot be read stays empty and the row is flagged REVIEW. Every written row
carries Source Page for traceability.

Usage:
  python3 map_sheets.py OCR.json COPY_SHEET_ID [--dry-run] [--dump dump.json]
"""
import json, re, sys, os, collections

def g(t, pat):
    m = re.search(pat, t, re.I)
    if not m:
        return ""
    return (m.group(1) or "").strip() if m.groups() else m.group(0).strip()

# ---------------- document classification (mutually exclusive, page-level) ---
def classify(t):
    u = t.upper()
    if re.search(r'^\s*FAKTUR PENJUALAN\b', t, re.M):
        return 'FAKTUR_PENJUALAN'
    if 'RECEIVING SLIP ORDER' in u:
        return 'SLIP_MARIMARI'
    if 'GOODS RECEIVE NOTE' in u:
        return 'GRN_DFI'
    if 'RECEIVING NOTE' in u and 'SURAT JALAN' in u and 'TIP TOP' in u:
        return 'RJ_TIPTOP'
    if 'RECEIVING NOTE' in u and 'BUDI' in u:
        return 'RJ_BUDI'
    if 'RECEIVING MEMO' in u and '.PL.' in u:
        return 'RJ_MEMO'
    if 'GOOD RECEIPT SLIP' in u:
        return 'GR_SLIP'
    if re.search(r'\bGOOD RECEIPT\b', u):
        return 'GR_BOOTS'
    if 'FAKTUR PAJAK' in u and not re.search(r'PURCHASE ORDER|WITH PURCHASE ORDER', u) \
       and re.search(r'^\s*(FAKTUR PAJAK|FORM 01)', t, re.M):
        return 'FAKTUR_PAJAK'
    if re.search(r'^(?:---)?PURCHASE ORDER(?:---)?\s*$|NO PO\s*:|NO\. PO\b|Nomor P\.O|P\.O No|ORDER NO\b', u, re.M) \
       or ' PURCHASE ORDER' in u:
        return 'PURCHASE_ORDER'
    return 'OTHER'

# ---------------- PO / receipt item parsers, one per vendor layout ----------
def _numbers(lines):
    return [l.strip() for l in lines if re.fullmatch(r'-?[\d.,]{1,20}%?', l.strip())]

def po_boots_vertical(t, page):
    """PANEN SELARAS (Boots/Farmers): item blocks start at line `00010`.
    Layout per block: sku / barcode / description / [Country..] / qty / UOM / d1 d2 d3 / [PurcPrice] / UntPrice / Total."""
    po = g(t, r'Purchase Order\s*\n\s*([A-Z0-9./-]+)') or g(t, r'P\.O No\s*:\s*([^\n]+)')
    vendor = g(t, r'Purchase Order\s*\n\s*[A-Z0-9./-]+\s*\n\s*([^\n]+)')
    rows = []
    lines = t.splitlines()
    idxs = [i for i, l in enumerate(lines) if re.fullmatch(r'0{2,4}\d{1,4}', l.strip()) and int(l.strip()) % 10 == 0 and int(l.strip()) > 0]
    for k, i in enumerate(idxs):
        end_i = idxs[k+1] if k+1 < len(idxs) else len(lines)
        block = [l.strip() for l in lines[i+1:end_i] if l.strip()]
        sku = next((b for b in block if re.fullmatch(r'[A-Z0-9]{10,16}', b) and not b.isdigit() and re.search(r'[A-Z]', b)), '')
        desc = next((b for b in block if re.match(r'^[A-Z][A-Z0-9/&\' .+-]{5,}\s+\S', b) and 'Country' not in b and b != sku and not b.isdigit()), '')
        uom_pos = next((j for j, b in enumerate(block) if re.fullmatch(r'[A-Z]{1,4}', b) and b not in ('HL', 'PT', 'TBK') and not re.fullmatch(r'[A-Z]\d{9,15}', b)), None)
        if uom_pos is None or uom_pos == 0:
            continue
        qty = re.sub(r'[.,]0+$', '', block[uom_pos-1]) if re.fullmatch(r'[\d.,]+', block[uom_pos-1]) else ''
        if not qty:
            continue
        tail = _block_numbers(block[uom_pos+1:], 10)
        d = [x for x in tail[:3] if x == '0']
        total = tail[-1] if tail else ''
        def _val(v):
            try:
                return float(re.sub(r'[.,](?=\d{3}\b)|[.,](?=\d{3}$)', '', v)) if len(v.replace(',','').replace('.','')) > 3 else float(v.replace(',', '.'))
            except Exception:
                return None
        qv, tv = _val(qty), _val(total)
        price = tail[-2]
        for cand in (tail[-3], tail[-2]) if len(tail) >= 3 else (tail[-2],):
            pv = _val(cand)
            if qv and tv and pv and abs(qv * pv - tv) / max(tv, 1) < 0.02:
                price = cand
                break
        rows.append([po, vendor, '', (sku + ' ' + desc).strip(), qty, block[uom_pos], price, ','.join(d), total, page, 'OCR_ITEM'])
    return rows

def po_farmers_horizontal(t, page):
    """Ranch/Farmers/SCS: `10 30335668 HL 1,00 KAR/12/ 451.892 0 451.892`."""
    po = g(t, r'P\.O No\s*:\s*([^\n]+)') or g(t, r'JAKA\s*:\s*([^\n]+)') or g(t, r'Ref\s*:\s*([^\n]+)')
    vendor = g(t, r'Name\s*:\s*([^\n]+)')
    rows = []
    lines = t.splitlines()
    for i, l in enumerate(lines):
        m = re.match(r'^\s*\d{1,3}\s+(\d{7,})\s+(HL|NH|HF|NF)\s+([\d.,]+)\s+([A-Z]{1,6}/[\d.,/]+|EA|PAC|KTN|CTNS?)\s+([\d.,]+)\s+(-?[\d.,]+)\s+([\d.,]+)\s*$', l)
        if not m:
            continue
        code, _, qty, uom, price, disc, total = m.groups()
        desc = ''
        for j in range(i+1, min(i+4, len(lines))):
            s = lines[j].strip()
            if re.match(r'^\(\d{10,14}\)$', s):
                continue
            if s and not re.match(r'^\s*\d{1,3}\s+\d{7,}', lines[j]) and not re.search(r'PO Creation|Total|Discount', s, re.I):
                desc = s
                break
        rows.append([po, vendor, '', (code + ' ' + desc).strip(), qty, uom, price, disc, total, page, 'OCR_ITEM'])
    return rows

def po_dfj_vertical(t, page):
    """DFI Retail: strict vertical item blocks after the NO./PLU(BAR) table."""
    po = g(t, r'NO PO\s*:\s*([^\n]+)')
    vendor = g(t, r'\d{5,6}\s+(SARANA[^\n]+)')
    rows = []
    lines = [l.strip() for l in t.splitlines()]
    starts = [i for i, l in enumerate(lines)
              if re.fullmatch(r'\d{1,2}', l) and i+1 < len(lines)
              and re.match(r'^\d{5,}(?:\(\d{12,14}\))?$', lines[i+1])]
    stop_re = re.compile(r'^(PURCHASE|TOTAL|DPP|PPN|NON-RET|DISTRIBUTION|INTRO|SPEC|DIST|TRADE|FULL|HARGA|ISI|JUMLAH|NAMA|NO\.|\(SLP\)|\(TGL)', re.I)
    for k, i in enumerate(starts):
        end = starts[k+1] if k+1 < len(starts) else len(lines)
        block = []
        for b in lines[i+1:end]:
            if not b or stop_re.match(b) or 'IDR' in b.upper():
                break
            block.append(b)
        if len(block) < 5:
            continue
        plu = block[0]
        flags = [j for j, b in enumerate(block) if b in ('Y', 'N')]
        if not flags:
            continue
        dstart = flags[1] + 1 if len(flags) >= 2 else flags[0] + 2
        desc = []
        j = 1
        while j < len(block) and block[j] not in ('Y', 'N'):
            if not re.match(r'^\d+$', block[j]):
                desc.append(block[j])
            j += 1
        rest = block[dstart:] if dstart < len(block) else []
        qty = next((b for b in rest if re.match(r'^\d[\d.,]*\s+(CT|PAC|BGN|PCS|EA|BOX|KRK)$', b, re.I)), '')
        sat = next((b for b in rest if re.search(r'\bEA\b', b) and b != qty), '')
        pcts = [b for b in rest if b.endswith('%')]
        money = [b for b in rest if re.fullmatch(r'\d{1,3}(?:\.\d{3})+(?:,\d{2})?', b)]
        unit = money[0] if money else ''
        total = money[-1] if money else ''
        rows.append([po, vendor, '', (plu + ' ' + ' '.join(desc)).strip(), qty,
                     (qty.split()[-1] if qty else ''), unit, ' '.join(pcts[:2]), total, page, 'OCR_ITEM'])
    return rows

def po_tiptop_po(t, page):
    """TipTop PO order lines: '1 0070571 DESC Disc: 3.00% 03 500ML LSN <barcode> <qty> <price> <total>'."""
    po = g(t, r'([0-9]{4}\.PO\.\d{2}\.\d{6})')
    vendor_m = re.search(r'SARANA ABADI MAKMUR BERSAMA PT \((\d+)\)', t)
    vendor = ('SARANA ABADI MAKMUR BERSAMA PT (' + vendor_m.group(1) + ')') if vendor_m else ''
    rows = []
    for line in t.splitlines():
        m = re.match(r'^\s*(\d{1,2})\s+(\d{7})\s+(.+?)\s+(?:Disc:\s*[\d.]+%\s+)?\d{0,2}\s+(\S{2,8})\s+([A-Z0-9]{2,4})\s+(\d{13})\s+(\d+)\s+([\d.,]+)\s+([\d.,]+)\s*$', line)
        if m:
            idx, sku, desc, size, uom, barcode, qty, price, total = m.groups()
            rows.append([po, vendor, '', (sku + ' ' + desc + ' ' + size).strip(), qty, uom, price, '', total, page, 'OCR_ITEM'])
    return rows

def po_dfj_horizontal(t, page):
    """DFI variant: full 17-col row inline, Indonesian money separators."""
    po = g(t, r'NO PO\s*:\s*([^\n]+)')
    vendor = g(t, r'\d{5,6}\s+(SARANA[^\n]+)')
    rows = []
    for line in t.splitlines():
        m = re.match(r'^\s*(\d{1,2})\s+(\d{5,}(?:\(?\d{12,14}\)?)?)\s+(.+?)\s*(?:[YN]\s+[YN]\s+)?(\d[\d.,]*)\s+(CT|PAC|BGN|KRK|BOX)\s+\d+\s*x\s*\d+\s+(\d[\d.,]*)\s*EA\s+(\d+)\s+([\d.,]+)\s+((?:[\d.,]+%\s+)+)([\d.,]+)\s+([\d.,]+)\s*$', line)
        if m:
            idx, plu, desc, qty, uom, sat, pallet, unit, pcts, gross, slp = m.groups()
            if len(plu) > 9:
                plu, _bc = plu[:7], plu[7:]
            rows.append([po, vendor, '', (plu + ' ' + desc).strip(), qty, uom, unit, ' '.join(pcts.split()[:2]), slp, page, 'OCR_ITEM'])
    return rows

def po_boots_inline(t, page):
    """Boots/Farmers single-line item variant with barcode on next line."""
    po = g(t, r'Purchase Order\s*\n\s*([A-Z0-9./-]+)')
    vendor = g(t, r'Purchase Order\s*\n\s*[A-Z0-9./-]+\s*\n\s*([^\n]+)')
    rows = []
    lines = t.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r'^\s*(0{2,4}\d{1,4})\s+([A-Z]?\d{9,12})\s+(.+?)\s+(\d[\d.,]*)\s+(EA|PAC|BTL|CTN)\s+(?:-?[\d.,]+\s+){2,4}([\d.,]+)\s+([\d.,]+)\s*$', line)
        if not m:
            continue
        idx, sku, desc, qty, uom, price, total = m.groups()
        rows.append([po, vendor, '', (sku + ' ' + desc).strip(), qty, uom, price, '', total, page, 'OCR_ITEM'])
    return rows

def po_kalimalang(t, page):
    """Kalimalang PO: 'N code DESC QTY UOM price QTY2 UOM2 price2 disc subtotal'."""
    po = g(t, r'No Pemesanan\s*:\s*([^\n]+)')
    vendor = g(t, r'Supplier\s*:\s*([^\n]+)')
    rows = []
    for line in t.splitlines():
        m = re.match(r'^\s*(\d{1,2})\s+(\d{11,14})\s+(.+?)\s+(\d+)\s+(DUS|PCS|BTL|KLG|BKS)\s+([\d.,]+)\s+(\d+)\s+(DUS|PCS|BTL|KLG|BKS)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s*$', line)
        if m:
            idx, code, desc, q1, u1, p1, q2, u2, p2, disc, subtotal = m.groups()
            rows.append([po, vendor, '', (code + ' ' + desc).strip(), q2, u2, p2, disc, subtotal, page, 'OCR_ITEM'])
    return rows

def po_bpb(t, page):
    """Unilever BPB/SJ: 'N plu desc pack QTY/0 bonus' — qty in carton fraction."""
    po = g(t, r'No\. PO\s*:\s*([^\n]+)')
    sj = g(t, r'No\. F/SJ/NPB\s*:\s*([^\n]+)')
    rows = []
    for line in t.splitlines():
        m = re.match(r'^\s*(\d{1,2})\s+(\d{7,8})\s+(.+?)\s+(\d+/\d+)\s+(\d+)\s*$', line)
        if m:
            idx, plu, desc, qty, bonus = m.groups()
            rows.append([po, '', '', (plu + ' ' + desc).strip(), qty, 'CTN', '', '', '', page, 'OCR_ITEM'])
    return rows, sj

def tt_budi(p):
    """PT Budi receiving note (also references Surat Jalan DO#)."""
    t = p.get('text') or ''
    date, doc = g(t, r'Receiving#?\s*:\s*([^\n]+)'), g(t, r'([0-9]{4}\.RC\.[\d.]+)')
    po, do = g(t, r'(?:9918|0000)\.PL\.[\d.]+'), g(t, r'DO#\s*:\s*([^\n]+)')
    rows = []
    for line in t.splitlines():
        m = re.match(r'^\s*(\d{1,2})\s+(\S+\.PL\.\d{2}\.\d+)\s+(\d{8,})\s+(\d{6,})\s+(.+?)\s+(\S+)?\s+(\d+)\s+(CRT/\d+|PAC|\d+)\s*$', line)
        if m:
            idx, pl, sku12, sku7, desc, size, qty, uom = m.groups()
            rows.append([date, doc, pl, '', sku7, (desc + ' ' + size).strip(), qty, uom, p['page'], 'OCR_ITEM'])
    sj = [[do, date, doc, p['page'], 'OCR_ITEM']] if do else []
    return rows, sj

def po_dfj_inline(t, page):
    """DFI OCR keeps the 17-col row on one pipe-separated line."""
    po = g(t, r'NO PO\s*:\s*([^\n]+)')
    vendor = g(t, r'\d{5,6}\s+(SARANA[^\n]+)')
    rows = []
    for line in t.splitlines():
        parts = [p.strip() for p in line.split('|')]
        if len(parts) < 8 or not re.fullmatch(r'\d{1,2}', parts[0]) or not re.match(r'^\d{5,}', parts[1]):
            continue
        cells = parts[1:]
        plu = cells[0]
        desc_cells = []
        rest = []
        for c in cells[1:]:
            if c in ('Y', 'N'):
                break
            desc_cells.append(c)
        flag_after = cells[1+len(desc_cells):]
        qty = next((c for c in flag_after if re.match(r'^\d[\d.,]*\s+\S+$', c)), '')
        money = [c for c in flag_after if re.fullmatch(r'\d{1,3}(?:\.\d{3})+(?:\.\d{2})?', c)]
        pcts = [c for c in flag_after if c.endswith('%')]
        unit = money[0] if money else ''
        total = money[-1] if money else ''
        rows.append([po, vendor, '', (plu + ' ' + ' '.join(desc_cells)).strip(), qty,
                     qty.split()[-1] if qty else '', unit, ' '.join(pcts[:2]), total, page, 'OCR_ITEM'])
    return rows

def _block_numbers(block, n):
    out = []
    for b in block:
        if re.fullmatch(r'[\d.,]+', b):
            # skip pure-digit long numbers (barcodes), they are not quantities
            if re.fullmatch(r'\d{9,}', b):
                continue
            out.append(b.rstrip('.,'))
            if len(out) >= n:
                break
    return out

def po_primafood(t, page):
    """PrimaFood: vertical block idx / SKU(8) / desc / pack / qty / price / d1-d4 / total."""
    po = g(t, r'Nomor P\.O\s*:\s*([^\n]+)')
    vendor = g(t, r'Vendor:\s*\n([^\n]+)')
    rows = []
    lines = [l.strip() for l in t.splitlines()]
    starts = [i for i, l in enumerate(lines) if re.fullmatch(r'\d{1,2}', l)
              and i+2 < len(lines) and re.fullmatch(r'\d{8}', lines[i+1])]
    for k, i in enumerate(starts):
        end = starts[k+1] if k+1 < len(starts) else min(i+15, len(lines))
        block = [b for b in lines[i+1:end] if b]
        sku = block[0] if block else ''
        desc = next((b for b in block[1:5] if not re.fullmatch(r'[A-Za-z]{3}', b) and not re.fullmatch(r'-?[\d.,]+', b)), '')
        nums = _block_numbers(block[2:], 7)
        qty = nums[0] if nums else ''
        price = nums[1] if len(nums) > 1 else ''
        disc = nums[2] if len(nums) > 2 else ''
        total = nums[-1] if nums else ''
        rows.append([po, vendor, '', (sku + ' ' + desc).strip(), qty, '', price, disc, total, page, 'OCR_ITEM'])
    return rows

def po_yogya(t, page):
    """Yogya: vertical block idx / 14-digit code / subclass / ext / desc / qty / [1: / 0] / price / d1-d4 / total."""
    po = g(t, r'ORDER NO\s*\n?\s*(\d{10,})') or g(t, r'ORDER NO\b[^\d]*(\d+)')
    vendor = ''
    rows = []
    lines = [l.strip() for l in t.splitlines()]
    starts = [i for i, l in enumerate(lines) if re.fullmatch(r'\d{1,2}', l)
              and i+4 < len(lines) and re.fullmatch(r'\d{14}', lines[i+1])]
    for k, i in enumerate(starts):
        end = starts[k+1] if k+1 < len(starts) else min(i+20, len(lines))
        block = [b for b in lines[i+1:end] if b]
        code = block[0] if block else ''
        ext = block[2] if len(block) > 2 and re.fullmatch(r'\d{6,8}', block[2]) else ''
        desc = next((b for b in block[3:6] if not re.fullmatch(r'-?[\d.,:%]+', b) and b not in ('1:',)), '')
        nums = _block_numbers(block[3:], 8)
        qty = nums[0] if nums else ''
        price = next((x for x in nums[1:5] if re.search(r'\.\d{2}$', x) and float(x.replace(',', '')) > 1000), '')
        total = next((x for x in reversed(nums) if re.search(r'\.\d{2}$', x) and float(x.replace(',', '')) >= float(price.replace(',', '') or 0)), '') if price else ''
        rows.append([po, '', '', (code + ' ' + desc).strip(), qty, '', price, '', total, page, 'OCR_ITEM'])
    return rows

def po_tiptop(t, page):
    """TipTop PO: 'N code desc size CTN12 qty price total'."""
    po = g(t, r'([0-9]{4}\.PO\.\d{2}\.\d+)')
    vendor = g(t, r'SARANA ABADI MAKMUR BERSAMA PT \((\d+)\)')
    rows = []
    for line in t.splitlines():
        m = re.match(r'^\s*(\d{1,2})\s+(\d{7,})\s+(.+?)\s+((?:\d+\s*)?(?:CTN\d+|LTR\d+|PAC\d*|[A-Z]{2,4}\d*))\s+(\d+)\s+([\d.,]+)\s+([\d.,]+)\s*$', line)
        if m:
            idx, code, desc, size, qty, price, total = m.groups()
            rows.append([po, vendor, '', (code + ' ' + desc + ' ' + size).strip(), qty, 'CTN', price, '', total, page, 'OCR_ITEM'])
    return rows

def po_primafood_pipe(t, page):
    """PrimaFood pipe-table variant: No | SKU | Barcode | Desc | UOM | Qty | Harga | Disc.. | Line Amount."""
    po = g(t, r'Nomor P\.O\s*:\s*([^\n]+)')
    vendor = g(t, r'Vendor:\s*\n\s*(\S[^\n]+)')
    rows = []
    for line in t.splitlines():
        raw = [p.strip() for p in line.split('|')]
        parts = [p for p in raw if p and not re.fullmatch(r'\|+', p)]
        if len(parts) < 8 or not re.fullmatch(r'\d{1,2}', parts[0]) or not re.fullmatch(r'\d{8}', parts[1]):
            continue
        sku, desc, uom, qty, price = parts[1], parts[2], parts[3], parts[4], parts[5]
        discs = [p for p in parts[6:-1] if re.fullmatch(r'[\d.]+', p)]
        amount = parts[-1]
        if not re.fullmatch(r'[\d,.]+', amount):
            continue
        rows.append([po, vendor, '', (sku + ' ' + desc).strip(), qty, uom, price, ' '.join(discs[:2]), amount, page, 'OCR_ITEM'])
    return rows

def po_aeon(t, page):
    """AEON vertical: idx / desc / pack / CARTON / packqty / itemno / barcode / qty / disc / '-' / unitprice / amount."""
    po = g(t, r'(?i)PO\s+(?:No\.?)?\s*[:\n]\s*(\d{6,})') or g(t, r'\n(7\d{8})\n')
    vendor = 'PT. SARANA ABADI MAKMUR BERSAMA'
    rows = []
    lines = [l.strip() for l in t.splitlines()]
    for i, l in enumerate(lines):
        if not re.fullmatch(r'\d{1,2}', l):
            continue
        blk = lines[i+1:i+13]
        if len(blk) < 11 or not re.match(r'^[A-Z0-9 /&\'().+-]{6,}$', blk[0] or 'x'):
            continue
        desc = blk[0]
        if not re.fullmatch(r'\d{2,3}\.\d{2}', blk[1] or 'x') or (blk[2] or '').upper() not in ('CARTON', 'PCS', 'CTN'):
            continue
        itemno = next((b for b in blk[4:7] if re.fullmatch(r'\d{8}', b)), '')
        tail = [b for b in blk[5:] if re.fullmatch(r'\d[\d.,]*|-', b)]
        qty = next((b for b in tail if re.search(r'\.\d{2}$', b) and float(b[:-3]) < 10000), '')
        price = next((b for b in tail if re.fullmatch(r'\d{1,3}(,\d{3})*\.\d{2}', b) and b != qty and b != '0.00'), '')
        amount = next((b for b in reversed(tail) if b not in ('-', price, qty)), '')
        if qty and price and amount:
            rows.append([po, vendor, '', (itemno + ' ' + desc).strip(), qty, blk[2], price, '', amount, page, 'OCR_ITEM'])
    return rows

def po_mitra(t, page):
    """Mitra Belanja Anda: 'N sku9 desc BOX/n barcode qty [tick] price total'."""
    po = g(t, r'PR No\s*[:\n]\s*(\d{6,})') or g(t, r'PO\s*#?\s*[:\n]\s*(\d{6,})')
    rows = []
    for line in t.splitlines():
        m = re.match(r'^\s*(\d{1,2})\s+(\d{9})\s+(.+?)\s+(BOX/\d+|CTN/\d+|[A-Z]+/\d+)\s+(\d{13})\s+(\d+)\s+\S?\s*([\d.,]+)\s+([\d.,]+)\s*$', line)
        if m:
            idx, sku, desc, pack, barcode, qty, price, total = m.groups()
            rows.append([po, 'PT. MITRA BELANJA ANDA', '', (sku + ' ' + desc).strip(), qty, pack, price, '', total, page, 'OCR_ITEM'])
    return rows

def tt_memo(p):
    """Indofood 'Receiving Memo': 'N 9918.PL.. sku11 sku7 DESC SIZE qty CRT/n' + DO# header."""
    t = p.get('text') or ''
    date = g(t, r'Date\s*:\s*([^\n]+)')
    doc = g(t, r'Receiving#?\s*:\s*([^\n]+)')
    do = g(t, r'DO#?\s*:\s*([^\n]+)')
    rows = []
    for line in t.splitlines():
        m = re.match(r'^\s*(\d{1,2})\s+(\d{4}\.PL\.\d{2}\.\d+)\s+(\d{9,11})\s+(\d{7})\s+(.+?)\s+(\d+)\s+(CRT/\d+|CTN/\d+|PAC)\s*$', line)
        if m:
            idx, pl, sku11, sku7, desc, qty, uom = m.groups()
            rows.append([date, doc, pl, '', sku7, desc.strip(), qty, uom, p['page'], 'OCR_ITEM'])
    sj = [[do, date, doc, p['page'], 'OCR_ITEM']] if do and rows else []
    return rows, sj

# ---------------- Faktur Penjualan (own table schema) ------------------------
def inv_faktur_penjualan(p):
    t = p.get('text') or ''
    sor = g(t, r'Sales Order\s*(?:\(SO\)|\[SO\])?\s*#?\s*:\s*([^\n]+)')
    out = []
    lines = t.splitlines()
    for ix, line in enumerate(lines):
        m = re.match(r'^\s*(\d{7})\s+(.+?)\s+(\d+\s*/\s*\d+)\s+(.+?)\s*$', line)
        if not m:
            continue
        vals = re.findall(r'(?<![A-Za-z])\d[\d.,]*', m.group(4))
        if len(vals) < 7:
            continue
        price, d1, d2, d3, d4, d5, amount = vals[-7:]
        desc = re.sub(r'^\d{4,10}\s+', '', m.group(2).strip())
        if ix+1 < len(lines) and lines[ix+1].strip() and not re.match(r'^\s*\d{7}\s+|^Total|Dasar|PPn|TOTAL|Subtotal|Cap|Jumlah', lines[ix+1].strip(), re.I):
            desc += ' ' + lines[ix+1].strip()
        pm = re.search(r'((?:\d+\s*[Xx]\s*)+[\d.]+\s*(?:ML|GR|G|KG|L|CC)|\d+\s*[Xx]\s*\d+\s*(?:ML|GR|G|KG|L))$', desc)
        pack = pm.group(1) if pm else ''
        name = desc[:pm.start()].strip() if pm else desc
        nv = p.get('numeric_verification') or {}
        status = nv.get('status') or 'REVIEW'
        conf = 'high' if status == 'PASS' else 'medium'
        out.append([m.group(1), sor, pack, name, m.group(3), price, d1, d2, d3, d4, d5, amount,
                    '', '', '', p['page'], conf, status])
    dpp = g(t, r'(?:Dasar Pengenaan Pajak|DPP)\s*:?\s*([\d.,]+)')
    ppn = g(t, r'PPn?\s*:?\s*([\d.,]+)')
    total = g(t, r'TOTAL\s*:?\s*([\d.,]+)')
    if dpp or ppn or total:
        out.append(['', sor, '', '[PAGE TOTAL]', '', '', '', '', '', '', '', '', dpp, ppn, total,
                    p['page'], 'high', 'SUMMARY'])
    return out

# ---------------- receipt-slip parsers (Tanda Terima schema) -----------------
def tt_marimari(p):
    t = p.get('text') or ''
    date, doc = g(t, r'Tgl Terima\s*:\s*([^\n]+)'), g(t, r'No Receive\s*:\s*([^\n]+)')
    po, vendor = g(t, r'No PO\s*:\s*([^\n]+)'), g(t, r'No PO\s*:\s*\d+\s*\n(\d{6})')
    rows = []
    for line in t.splitlines():
        m = re.match(r'^\s*\d+\s+(\d{6,})\s+(\d{12,14})\s+(.+?)\s+(\d+(?:\.\d+)?)\s+([A-Z]+)\s+(\d+)\s*(KTN|CTN|PC)\s+([\d.,]+)\s+(?:[\d.]+%\s+)?([\d.,]+)', line)
        if m:
            item, _, desc, _pq, _pu, qty, uom, _pr, _t = m.groups()
            rows.append([date, doc, po, vendor, item, desc.strip(), qty, uom, p['page'], 'OCR_ITEM'])
    if rows:
        return rows
    # block variant (Mari-mari): idx / itemcode / desc / qty.pc / <qty> KTN / price / disc% / total
    lines = [l.strip() for l in t.splitlines()]
    starts = [i for i, l in enumerate(lines) if re.fullmatch(r'\d{1,2}', l)
              and i + 1 < len(lines) and re.fullmatch(r'\d{6,}', lines[i+1])]
    for k, i in enumerate(starts):
        end = starts[k+1] if k+1 < len(starts) else min(i + 9, len(lines))
        block = [b for b in lines[i+1:end] if b]
        item = block[0] if block else ''
        desc = next((b for b in block[1:4] if not re.fullmatch(r'[\d.,]+\s*\w*', b) and not b.isdigit()), '')
        mq = next((re.match(r'(\d+(?:\.\d+)?)\s*(KTN|CTN|PC)', b) for b in block if re.match(r'^\d+(?:\.\d+)?\s+(KTN|CTN|PC)$', b)), None)
        mkr = next((re.match(r'^(\d+)\s+(KTN|CTN)$', b) for b in block), None)
        qty = mkr.group(1) if mkr else (mq.group(1) if mq else '')
        uom = (mkr.group(2) if mkr else (mq.group(2) if mq else ''))
        if item and desc and qty:
            rows.append([date, doc, po, vendor, item, desc, qty, uom, p['page'], 'OCR_ITEM'])
    return rows

def tt_gr_slip(p):
    """Farmers/Indogrosir/Grandlucky 'Good Receipt Slip': 'N item HL qty EA POIt itemline' + desc line."""
    t = p.get('text') or ''
    date = g(t, r'Posting Date\s*:\s*([^\n]+)')
    doc = g(t, r'Document No\.?\s*:\s*([^\n]+)')
    po = g(t, r'Purchase Order\s*:\s*([^\n]+)')
    m = re.search(r'Vendor Number\s*:\s*(\d+)\s*\n([^\n]+)', t)
    vendor = (m.group(1) + ' ' + m.group(2).strip()) if m else ''
    rows = []
    lines = t.splitlines()
    for i, line in enumerate(lines):
        m2 = re.match(r'^\s*(\d{1,2})\s+(\d{6,8})\s+(HL|IS|NH|HF)?\s*(\d+(?:\.\d+)?)\s+(EA|PAC|CTN|KTN)\s+(\d{4})\s+(\d{5})\s*$', line)
        if not m2:
            continue
        idx, item, _hl, _pq, _pu, poit, poitem = m2.groups()
        qty = _numbers([l.strip() for l in lines[i+1:i+3]])
        # qty already on the same line as parsed _pq
        desc = ''
        for b in lines[i+1:i+3]:
            bs = b.strip()
            if bs and not re.fullmatch(r'[\d.,:]+', bs) and 'Status' not in bs and 'Page' not in bs and 'MART' not in bs:
                desc = bs; break
        rows.append([date, doc, po, vendor, item, desc, _pq, _pu, p['page'], 'OCR_ITEM'])
    return rows

def tt_gr_boots(p):
    t = p.get('text') or ''
    date = g(t, r'GR Date\s*:\s*([^\n]+)')
    doc = g(t, r'Good Receipt\s*\n\s*([A-Z0-9./-]+)')
    po = g(t, r'Ref\. PO No\.\s*:\s*([^\n]+)') or g(t, r'GR PO\s+([A-Z0-9]+)')
    vendor = g(t, r'^(PT\.? [A-Z][^\n]+)$', ) if re.search(r'^PT\.? [A-Z]', t, re.M) else ''
    rows = []
    lines = t.splitlines()
    # GR items may be inline rows ("0001 SKU barcode DESC ... 4 EA 0 0 0 63,361 253,444")
    for line in lines:
        m = re.match(r'^\s*(0{3}\d)\s+([A-Z0-9]{8,16})\s+(\d{13})?\s*(.+?)\s+Country of Origin\s*:\s*[^\s]*\s+(\d[\d.,]*)\s+([A-Z]+)\s+((?:-?[\d.,]+\s+){1,5}?)(-?[\d.,]+)\s+([\d.,]+)\s*$', line)
        if m:
            idx, sku, barcode, desc, qty, uom, _mid, price, total = m.groups()
            rows.append([date, doc, po, barcode or sku, sku, desc.strip(), qty, uom, p['page'], 'OCR_ITEM'])
    if rows:
        return rows
    idxs = [i for i, l in enumerate(lines) if re.fullmatch(r'0{3}\d', l.strip())]
    for k, i in enumerate(idxs):
        end = idxs[k+1] if k+1 < len(idxs) else len(lines)
        block = [l.strip() for l in lines[i+1:end] if l.strip()]
        sku = next((b for b in block if re.fullmatch(r'[A-Z0-9]{8,16}', b) and not b.isdigit()), '')
        barcode = next((b for b in block if re.fullmatch(r'\d{13}', b)), '')
        desc = next((b for b in block if re.match(r'^[A-Z][A-Z0-9/&\' +.-]{8,}$', b) and not b.startswith('Country')), '')
        nums = _numbers(block)
        uom = next((b for b in block if b in ('EA', 'PAC', 'CTN')), '')
        qty = nums[0] if nums else ''
        rows.append([date, doc, po, barcode, (sku + ' ' + desc).strip(), '', qty, uom, p['page'], 'OCR_ITEM'])
    return rows

def tt_grn_dfi(p):
    t = p.get('text') or ''
    date = g(t, r'TGL KONFIRMASI GRN\s*:\s*([^\n]+)')
    doc = g(t, r'NO GRN\s*:\s*([^\n]+)')
    po = g(t, r'NO PO\s*:\s*([^\n]+)')
    ref = g(t, r'NO REFERENCE\s*:\s*([^\n]+)')
    rows = []
    lines = t.splitlines()
    merged = []
    i = 0
    while i < len(lines):
        l = lines[i].rstrip()
        if re.match(r'^\s*\d{1,2}\s+\d{5,}(?:\(\d{12,14}\))?\s*$', l):
            j = i + 1
            while j < len(lines) and j < i + 4:
                l = (l + ' ' + lines[j].strip()).strip()
                j += 1
                if re.search(r'[YN]\s+[YN]\s+\S', l):
                    break
            merged.append(l); i = j; continue
        merged.append(l); i += 1
    for line in merged:
        m = re.match(r'^\s*(\d{1,2})\s+(\d{5,}(?:\(\d{12,14}\))?)\s*(.*?)\s*([YN])\s+([YN])\s+(.*)$', line)
        if not m:
            continue
        idx, plu, desc, _, _, rest = m.groups()
        desc = desc.strip()
        if not desc:
            k = merged.index(line)
            follow = []
            for b in merged[k+1:k+3]:
                if re.match(r'^\s*\d{1,2}\s+\d{5,}', b):
                    break
                follow.append(b.strip())
            desc = ' '.join(x for x in follow if not re.fullmatch(r'[\d.,]+', x) and 'Printed' not in x).strip()
        nums = re.findall(r'\d[\d.,]*', rest)
        m_ea = re.search(r'(\d[\d.,]*)\s*EA\s*(\d[\d.,]*)?', rest)
        qty = (m_ea.group(2) or m_ea.group(1)) if m_ea else (nums[1] if len(nums) > 1 else '')
        price = next((n for n in nums if re.search(r'\d,\d{2}$', n) or n.endswith('.00')), '')
        total = nums[-1] if nums else ''
        extra = ' | '.join(f'{k2}: {v}' for k2, v in (('Harga/KRT', price), ('Total Beli', total)) if v)
        rows.append([date, doc, po, ref, plu, (desc + ' ' + extra).strip(), qty, 'EA' if m_ea else '', p['page'], 'OCR_ITEM'])
    return rows

def tt_tiptop(p):
    t = p.get('text') or ''
    date = g(t, r'Tgl\. Receiving\s+([^\n]+)') or g(t, r'(\d{2}/\d{2}/\d{4})')
    doc = g(t, r'([0-9]{4}\.RC\.\d{2}\.\d+)')
    po = g(t, r'([0-9]{4}\.PO\.\d{2}\.\d+)')
    rows = []
    for line in t.splitlines():
        m = re.match(r'^\s*(\d{1,2})\s+(\d{4}\.PO\.\d{2}\.\d+)\s+(\d{6,})\s+(.+?)\s+([A-Z]{2,4})\s+(\d+)\s*$', line)
        if m:
            idx, _, sku, desc, uom, qty = m.groups()
            rows.append([date, doc, po, '', sku, desc.strip(), qty, uom, p['page'], 'OCR_ITEM'])
    return rows

def sj_tiptop(p):
    """TipTop receiving note also carries Surat Jalan (DO) number + date."""
    t = p.get('text') or ''
    do = g(t, r'(?:0001\.RC\.\d{2}\.\d+)\s+\d{2}/\d{2}/\d{4}\s+(\d+)\s+(\d{2}/\d{2}/\d{4})')
    m = re.search(r'([0-9]{4}\.RC\.[\d.]+)\s+(\d{2}/\d{2}/\d{4})\s+(\d+)\s+(\d{2}/\d{2}/\d{4})', t)
    if not m:
        return []
    rc, rcdate, do_no, do_date = m.groups()
    return [[do_no, do_date, rc, p['page'], 'OCR_ITEM']]

# ---------------- write -------------------------------------------------------
HEADERS = {
 'Faktur Penjualan': ['Kode Material','SOR','Kemasan','Nama Produk','Qty','Harga','Disc 1','Disc 2','Disc 3','Disc 4','Disc 5','Jumlah','Dasar Pengenaan Pajak','PPN','Total','Source Page','Confidence','Review Status'],
 'PO Customer': ['Purchase Order No','Vendor Code & name (Nama Lengkap SAMB)','PPN','Product Code & Description','Qty','UON','Unit Price','Discount','Total','Source Page','Mapping Status'],
 'Tanda Terima': ['Posting Date','Document No','Purchase Order No','Vendor Number','Item Code','Material Description','Qty','UON','Source Page','Mapping Status'],
 'Faktur Pajak': ['SOR','Billing Number','Kode Seri','NPWP & NITKU pengusaha & Pembeli','Dasar Pengenaan Pajak','PPN','Tanggal Transaksi','Source Page','Mapping Status'],
 'Dokumen Pelunasan': ['Reference No','Vendor Code & Name','Year, Month','Invoice Receipt Date','Store Code','Due','Amount','Text','Payment Document Number','Payment Date','Total Payment Amount','Customer Name','Source Page','Mapping Status'],
 'Surat Jalan': ['No Surat Jalan (DO)','Tanggal Surat Jalan','No Receiving','Source Page','Mapping Status'],
 'OCR Mapping Review': ['Source Page','Target Sheet','Classification Confidence','Numeric Status','Numeric Discrepancies','OCR Characters','OCR Excerpt','Mapping Status'],
}
GR_PO_HEADERS = ['Posting Date','Document No','Purchase Order No','Vendor Number','Item Code','Material Description','Qty','UON','Source Page','Mapping Status']
GRN_HEADERS = ['Posting Date','Document No','Purchase Order No','No Reference','Item Code','Material Description','Qty','Unit Price','Total','Source Page','Mapping Status']

def build(pages):
    sheets = collections.defaultdict(list)
    review = []
    for p in pages:
        t = p.get('text') or ''
        n = p['page']
        cat = classify(t)
        nv = p.get('numeric_verification') or {}
        ns = nv.get('status') or 'NOT_RUN'
        disc = '; '.join(map(str, (nv.get('discrepancies') or [])[:3]))[:300]
        if cat == 'FAKTUR_PENJUALAN':
            sheets['Faktur Penjualan'] += inv_faktur_penjualan(p)
            target, conf = 'Faktur Penjualan', 'HIGH'
        elif cat == 'SLIP_MARIMARI':
            r = tt_marimari(p)
            sheets['Tanda Terima'] += r
            target, conf = 'Tanda Terima', 'HIGH' if r else 'LOW'
        elif cat == 'GR_SLIP':
            r = tt_gr_slip(p)
            sheets['Tanda Terima'] += r
            target, conf = 'Tanda Terima (GR Slip)', 'HIGH' if r else 'LOW'
        elif cat == 'GR_BOOTS':
            r = tt_gr_boots(p)
            sheets['Tanda Terima'] += r
            if not r: sheets['Tanda Terima'].append(['', '', g(t, r'Ref\. PO No\.\s*:\s*([^\n]+)'), '', '', '', '', '', n, 'OCR_PAGE_REVIEW'])
            target, conf = 'Tanda Terima', 'HIGH'
        elif cat == 'GRN_DFI':
            r = tt_grn_dfi(p)
            sheets['Tanda Terima'] += r
            target, conf = 'Tanda Terima (GRN DFI)', 'HIGH' if r else 'LOW'
        elif cat == 'RJ_TIPTOP':
            sheets['Tanda Terima'] += tt_tiptop(p)
            sheets['Surat Jalan'] += sj_tiptop(p)
            target, conf = 'Tanda Terima + Surat Jalan', 'HIGH'
        elif cat == 'RJ_MEMO':
            r, sj = tt_memo(p)
            sheets['Tanda Terima'] += r
            sheets['Surat Jalan'] += sj
            target, conf = 'Tanda Terima + Surat Jalan', 'HIGH' if r else 'LOW'
        elif cat == 'RJ_BUDI':
            r, sj = tt_budi(p)
            sheets['Tanda Terima'] += r
            sheets['Surat Jalan'] += sj
            target, conf = 'Tanda Terima + Surat Jalan', 'HIGH' if r else 'LOW'
        elif cat == 'FAKTUR_PAJAK':
            target, conf = 'Faktur Pajak', 'HIGH'
            sheets['Faktur Pajak'].append([g(t, r'SOR[:\s]+([A-Z0-9]+)') or g(t, r'No Ref\.?\s*:\s*([^\n]+)'),
                                           g(t, r'No\.?\s*Faktur\s*:?\s*([^\n]+)') or g(t, r'Billing\s*[:\s]+([^\n]+)'),
                                           g(t, r'Kode Seri\s*[:\s]*([^\n]+)'), g(t, r'NPWP\s*[:\s]+([0-9.\-]+)'),
                                           g(t, r'DPP\s*[:\s]*([\d.,]+)'), g(t, r'PPN\s*[:\s]*([\d.,]+)'),
                                           g(t, r'Tanggal (?:Transaksi|Faktur)\s*[:\s]*([^\n]+)'), n, 'OCR_PAGE_REVIEW'])
        elif cat == 'PURCHASE_ORDER':
            bpb_rows, bpb_sj = po_bpb(t, n)
            rows = (po_primafood_pipe(t, n) or po_farmers_horizontal(t, n) or po_dfj_inline(t, n) or po_dfj_horizontal(t, n)
                    or po_tiptop_po(t, n) or po_boots_inline(t, n) or po_aeon(t, n)
                    or po_dfj_vertical(t, n) or po_boots_vertical(t, n) or po_tiptop(t, n)
                    or po_mitra(t, n) or po_primafood(t, n) or po_yogya(t, n) or po_kalimalang(t, n) or bpb_rows)
            if not rows and re.search(r'NO PO\s*:|PLU\(BAR\)|TGL PO', t, re.I):
                rows = po_dfj_vertical(t, n)
            if not rows and re.search(r'ORDER NO|ORDER DATE', u2 := t.upper()):
                rows = po_yogya(t, n)
            sheets['PO Customer'] += rows
            if not rows:
                po = g(t, r'P\.O No\s*:\s*([^\n]+)') or g(t, r'NO PO\s*:\s*([^\n]+)') or g(t, r'Nomor P\.O\s*:\s*([^\n]+)') or g(t, r'([0-9]{4}\.PO\.[\d.]+)') or g(t, r'ORDER NO\s*\n\s*([A-Z0-9]+)')
                sheets['PO Customer'].append([po, g(t, r'Vendor:\s*\n([^\n]+)') or g(t, r'Name\s*:\s*([^\n]+)'), '', '', '', '', '', '', '', n, 'OCR_PAGE_REVIEW'])
            target, conf = 'PO Customer', 'HIGH'
        else:
            target, conf = 'UNMAPPED - REVIEW', 'LOW'
        review.append([n, target, conf, ns, disc, len(t), ' '.join(t.split())[:400],
                       'MAPPED' if conf == 'HIGH' else 'REVIEW_REQUIRED'])
    return sheets, review

def col_name(n):
    s = ''
    while n:
        n, r = divmod(n-1, 26)
        s = chr(65+r) + s
    return s

def write(sid, sheets, review):
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build as apibuild
    d = json.load(open(os.path.expanduser('~/.hermes/google_token.json')))
    svc = apibuild('sheets', 'v4', credentials=Credentials.from_authorized_user_info(d), cache_discovery=False)
    meta = svc.spreadsheets().get(spreadsheetId=sid, fields='sheets(properties(title))').execute()
    titles = {x['properties']['title'] for x in meta['sheets']}
    added = []
    for title in list(sheets) + ['OCR Mapping Review']:
        if title not in titles:
            svc.spreadsheets().batchUpdate(spreadsheetId=sid, body={'requests': [{'addSheet': {'properties': {'title': title}}}]}).execute()
            added.append(title)
    tables = {k: v for k, v in sheets.items()}
    tables['OCR Mapping Review'] = review
    written = {}
    for title, hdr_key in [(t, t) for t in tables]:
        hdr = HEADERS.get(hdr_key, HEADERS['Tanda Terima'] if 'Terima' in hdr_key else ['Column%d' % (i+1) for i in range(10)])
        data = [(r[:len(hdr)] + ['']*len(hdr))[:len(hdr)] for r in tables[hdr_key]]
        safe = title.replace("'", "''")
        svc.spreadsheets().values().clear(spreadsheetId=sid, range="'%s'!A:Z" % safe, body={}).execute()
        svc.spreadsheets().values().update(spreadsheetId=sid,
            range="'%s'!A1:%s%d" % (safe, col_name(len(hdr)), len(data)+1),
            valueInputOption='RAW', body={'values': [hdr] + data}).execute()
        written[title] = len(data)
    return written

def main():
    src, sid = sys.argv[1], sys.argv[2]
    dry = '--dry-run' in sys.argv
    doc = json.load(open(src))
    std = doc.get('standard_json') or doc.get('standard') or doc
    pages = sorted(std.get('pages', []), key=lambda p: p.get('page', 0))
    sheets, review = build(pages)
    dump = next((sys.argv[sys.argv.index('--dump')+1] for a in sys.argv if a == '--dump'), None)
    if dump:
        json.dump({'sheets': sheets, 'review': review}, open(dump, 'w'), ensure_ascii=False, indent=1)
    print(json.dumps({k: len(v) for k, v in sheets.items()} | {'OCR Mapping Review': len(review)}, ensure_ascii=False, indent=1))
    if not dry:
        print('written:', json.dumps(write(sid, sheets, review), ensure_ascii=False))

if __name__ == '__main__':
    main()
