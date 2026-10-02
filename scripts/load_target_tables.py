#!/usr/bin/env python3
"""load_target_tables.py — write mapping dump (batch_map --dump JSON) into the
four destination tables (db-init/08): faktur_pajak, faktur_penjualan, po_customer,
tanda_terima. Idempotent: rows are replaced per (source_file) batch key.

Usage:
  python3 scripts/load_target_tables.py snapshots/dump_final_2026-10-01.json
  python3 scripts/load_target_tables.py --replace /tmp/bundle_dump.json
"""
import json, os, re, sys, datetime
import psycopg2

DATABASE_URL = os.environ.get('DATABASE_URL')  # set inside dp-extractor (@db:5432)


def to_num(v):
    """'1.551.000,00' / '24.00' / 2.0 / '2 / 0' -> float|None (IDR or plain)."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s:
        return None
    s = s.split('/')[0].strip()           # '2 / 0' -> '2'
    if not re.fullmatch(r'[-]?[\d.,]+', s):
        return None
    if ',' in s and ('.' in s or re.search(r',\d{2}$', s)):   # IDR style
        s = s.replace('.', '').replace(',', '.')
    else:
        s = s.replace(',', '')
    try:
        f = float(s)
    except ValueError:
        return None
    # OCR misreads of money/qty above ~1e12 are junk -> leave blank (no-guess rule)
    if abs(f) >= 1e12:
        return None
    return f


BULAN = {'januari': 1, 'februari': 2, 'maret': 3, 'april': 4, 'mei': 5, 'juni': 6,
         'juli': 7, 'agustus': 8, 'september': 9, 'oktober': 10, 'november': 11, 'desember': 12}


def to_date(v):
    if not v:
        return None
    s = str(v).strip().lower()
    m = re.match(r'(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})', s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        try:
            return datetime.date(y, mo, d)
        except ValueError:
            return None
    m = re.match(r'(\d{1,2})\s+([a-z]+)\s+(\d{4})', s)
    if m and m.group(2) in BULAN:
        try:
            return datetime.date(int(m.group(3)), BULAN[m.group(2)], int(m.group(1)))
        except ValueError:
            return None
    return None


def sp_num(v):
    m = re.search(r'p(\d+)', str(v or ''))
    return int(m.group(1)) if m else 0


def sp_text(v):
    return str(v) if v not in (None, '') else None


# per-tab mapping: dump row (list, sheet order) -> table kwargs
def map_faktur_pajak(r):
    return dict(sor=r[0], billing_number=r[1], kode_seri=r[2], npwp_pengusaha=r[3],
                dasar_pengenaan_pajak=to_num(r[4]), ppn=to_num(r[5]),
                tanggal_transaksi=to_date(r[6]), npwp_pembeli=r[7], nama_pembeli=r[8],
                nama_bkp=r[9], qty=to_num(r[10]), harga_satuan=to_num(r[11]),
                jumlah_harga=to_num(r[12]), potongan_harga=to_num(r[13]),
                uang_muka=to_num(r[14]), ppn_dev=to_num(r[15]), ppnbm=to_num(r[16]),
                harga_jual_total=to_num(r[17]), source_file=sp_text(r[18]) if len(r) > 18 else None)


def map_faktur_penjualan(r):
    return dict(kode_material=r[0], sor=r[1], kemasan=r[2], nama_produk=r[3],
                qty=to_num(r[4]), harga=to_num(r[5]), disc_1=to_num(r[6]),
                disc_2=to_num(r[7]), disc_3=to_num(r[8]), disc_4=to_num(r[9]),
                disc_5=to_num(r[10]), jumlah=to_num(r[11]),
                dasar_pengenaan_pajak=to_num(r[12]), ppn=to_num(r[13]), total=to_num(r[14]),
                source_page=sp_num(r[15]), confidence=r[16] if len(r) > 16 else None,
                review_status=r[17] if len(r) > 17 else None)


def map_po_customer(r):
    return dict(purchase_order_no=r[0], vendor_code=r[1] or None, po_issuer=r[2],
                ppn=r[3] or None, product_code=r[4] or None, product_name=r[5],
                qty=to_num(r[6]), uon=r[7] or None, unit_price=to_num(r[8]),
                discount=to_num(r[9]), total=to_num(r[10]), source_page=sp_num(r[11]),
                mapping_status=r[12] or 'MAPPED')


def map_tanda_terima(r):
    return dict(posting_date=to_date(r[0]), document_no=r[1], purchase_order_no=r[2],
                vendor_number=r[3] or None, item_code=r[4], material_description=r[5],
                qty=to_num(r[6]), uon=r[7] or None, source_page=sp_num(r[8]),
                mapping_status=r[9] or 'MAPPED')


TABS = [('Faktur Pajak', 'faktur_pajak', map_faktur_pajak),
        ('Faktur Penjualan', 'faktur_penjualan', map_faktur_penjualan),
        ('PO Customer', 'po_customer', map_po_customer),
        ('Tanda Terima', 'tanda_terima', map_tanda_terima)]

# document_id lookup: 10-digit token in source refs -> documents (by filename)
doc_cache = {}
def find_docid(conn, token):
    token = re.sub(r'\D', '', str(token or ''))
    if not token:
        return None
    if token in doc_cache:
        return doc_cache[token]
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM documents WHERE filename LIKE %s OR rel_path LIKE %s LIMIT 1",
                    (f'{token}%', f'%{token}%'))
        row = cur.fetchone()
    doc_cache[token] = row[0] if row else None
    return doc_cache[token]


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    dump_path = args[0] if args else 'snapshots/dump_final_2026-10-01.json'
    replace = '--replace' in sys.argv
    d = json.load(open(dump_path))
    conn = psycopg2.connect(DATABASE_URL)
    if replace:
        with conn, conn.cursor() as cur:
            for _, tbl, _ in TABS:
                cur.execute(f"DELETE FROM {tbl}")  # full reload only when asked
    stats = {}
    for tab, tbl, fnmap in TABS:
        rows = d.get('sheets', {}).get(tab, [])
        n = 0
        with conn, conn.cursor() as cur:
            for ri, r in enumerate(rows):
                kw = fnmap(r)
                src_ref = next((str(r[i]) for i in (18, 15, 11, 8) if i < len(r) and r[i]), None)
                # document_id from any 10-digit doc token in the raw source ref
                for token_src in (src_ref, str(r[0] if r else ''), kw.get('source_file')):
                    tok = re.search(r'(\d{10})', str(token_src or ''))
                    if tok:
                        did = find_docid(conn, tok.group(1))
                        if did:
                            kw['document_id'] = did
                            break
                if src_ref:
                    kw['rel_path'] = src_ref
                # natural key = stable content refs + position; re-running same dump = idempotent update
                key = f"{src_ref or tab}|{ri}"
                kw['natural_key'] = key
                cols = list(kw)
                placeholders = ', '.join(['%s'] * len(cols))
                upd = ', '.join(f"{c}=EXCLUDED.{c}" for c in cols if c != 'natural_key')
                vals = [kw[c] for c in cols]
                cur.execute(
                    f"INSERT INTO {tbl} ({', '.join(cols)}) VALUES ({placeholders}) "
                    f"ON CONFLICT (natural_key) DO UPDATE SET {upd}, updated_at=now()", vals)
                n += 1
        stats[tbl] = n
    with conn, conn.cursor() as cur:
        cur.execute("SELECT rel_path FROM (SELECT rel_path FROM faktur_pajak GROUP BY rel_path HAVING COUNT(*)>1) x")
    conn.close()
    print(json.dumps(stats, ensure_ascii=False))


if __name__ == '__main__':
    main()
