-- 08-target-mapping-tables.sql — the FOUR destination tables (focus: mapping output)
-- Mirrors the Google Sheet tabs 1:1. batch_map writes here first (DB = ledger),
-- then exports to the sheet copy. RPA/later consumers read these tables directly.
-- Migration-only: no runtime DDL in app code.

-- ============ TAB: Faktur Pajak (one row per Faktur Pajak document) ============
CREATE TABLE IF NOT EXISTS faktur_pajak (
  id                  BIGSERIAL PRIMARY KEY,
  document_id         TEXT REFERENCES documents(id) ON DELETE CASCADE,
  folder              TEXT,
  rel_path            TEXT,
  source_page         INTEGER NOT NULL DEFAULT 0,
  row_no              INTEGER NOT NULL DEFAULT 0,
  -- sheet columns, in sheet order:
  sor                 TEXT,
  billing_number      TEXT,
  kode_seri           TEXT,
  npwp_pengusaha      TEXT,          -- "NPWP & NITKU Pengusaha"
  dasar_pengenaan_pajak NUMERIC(16,2),
  ppn                 NUMERIC(16,2),
  tanggal_transaksi   DATE,
  npwp_pembeli        TEXT,          -- "NPWP & NITKU Pembeli"
  nama_pembeli        TEXT,
  nama_bkp            TEXT,          -- Nama Barang Kena Pajak
  qty                 NUMERIC(16,4),
  harga_satuan        NUMERIC(16,4),
  jumlah_harga        NUMERIC(16,2),
  potongan_harga      NUMERIC(16,2),
  uang_muka           NUMERIC(16,2),
  ppn_dev             NUMERIC(16,2),
  ppnbm               NUMERIC(16,2),
  harga_jual_total    NUMERIC(16,2),
  source_file         TEXT,
  -- workflow
  confidence          TEXT,          -- high|medium|low
  review_status       TEXT,          -- OK|REVIEW (reasons ';'-joined)
  mapping_status      TEXT NOT NULL DEFAULT 'MAPPED',  -- MAPPED|REVIEW|REVIEW-MISSING...
  exported_at         TIMESTAMPTZ,   -- set when written to the Google Sheet copy
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (document_id, source_page, row_no)
);
CREATE INDEX IF NOT EXISTS faktur_pajak_sor_idx   ON faktur_pajak (sor);
CREATE INDEX IF NOT EXISTS faktur_pajak_status_idx ON faktur_pajak (mapping_status, tanggal_transaksi);

-- ============ TAB: Faktur Penjualan (line items) ============
CREATE TABLE IF NOT EXISTS faktur_penjualan (
  id                  BIGSERIAL PRIMARY KEY,
  document_id         TEXT REFERENCES documents(id) ON DELETE CASCADE,
  folder              TEXT,
  rel_path            TEXT,
  source_page         INTEGER NOT NULL DEFAULT 0,
  row_no              INTEGER NOT NULL DEFAULT 0,
  kode_material       TEXT,
  sor                 TEXT,
  kemasan             TEXT,
  nama_produk         TEXT,
  qty                 NUMERIC(16,4),
  harga               NUMERIC(16,4),
  disc_1              NUMERIC(16,2),
  disc_2              NUMERIC(16,2),
  disc_3              NUMERIC(16,2),
  disc_4              NUMERIC(16,2),
  disc_5              NUMERIC(16,2),
  jumlah              NUMERIC(16,2),
  dasar_pengenaan_pajak NUMERIC(16,2),
  ppn                 NUMERIC(16,2),
  total               NUMERIC(16,2),
  source_file         TEXT,
  confidence          TEXT,
  review_status       TEXT,
  mapping_status      TEXT NOT NULL DEFAULT 'MAPPED',
  exported_at         TIMESTAMPTZ,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (document_id, source_page, row_no)
);
CREATE INDEX IF NOT EXISTS faktur_penjualan_sor_idx   ON faktur_penjualan (sor);
CREATE INDEX IF NOT EXISTS faktur_penjualan_status_idx ON faktur_penjualan (mapping_status);

-- ============ TAB: PO Customer (line items; every row must carry product name) ============
CREATE TABLE IF NOT EXISTS po_customer (
  id                  BIGSERIAL PRIMARY KEY,
  document_id         TEXT REFERENCES documents(id) ON DELETE CASCADE,
  folder              TEXT,
  rel_path            TEXT,
  source_page         INTEGER NOT NULL DEFAULT 0,
  row_no              INTEGER NOT NULL DEFAULT 0,
  purchase_order_no   TEXT,
  vendor_code         TEXT,          -- SAMB code @ client
  po_issuer           TEXT,          -- Customer PT (never SAMB)
  ppn                 TEXT,          -- '11%' | '1.1%'
  product_code        TEXT,
  product_name        TEXT NOT NULL, -- rule: never empty in this tab
  qty                 NUMERIC(16,4),
  uon                 TEXT,          -- unit of note / UOM
  unit_price          NUMERIC(16,4),
  discount            NUMERIC(16,2),
  total               NUMERIC(16,2),
  source_file         TEXT,
  confidence          TEXT,
  review_status       TEXT,
  mapping_status      TEXT NOT NULL DEFAULT 'MAPPED',
  exported_at         TIMESTAMPTZ,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (document_id, source_page, row_no)
);
CREATE INDEX IF NOT EXISTS po_customer_po_idx   ON po_customer (purchase_order_no);
CREATE INDEX IF NOT EXISTS po_customer_status_idx ON po_customer (mapping_status);

-- ============ TAB: Tanda Terima (GR/BPB receiving lines) ============
CREATE TABLE IF NOT EXISTS tanda_terima (
  id                  BIGSERIAL PRIMARY KEY,
  document_id         TEXT REFERENCES documents(id) ON DELETE CASCADE,
  folder              TEXT,
  rel_path            TEXT,
  source_page         INTEGER NOT NULL DEFAULT 0,
  row_no              INTEGER NOT NULL DEFAULT 0,
  posting_date        DATE,
  document_no         TEXT,
  purchase_order_no   TEXT,
  vendor_number       TEXT,
  item_code           TEXT,
  material_description TEXT,
  qty                 NUMERIC(16,4),
  uon                 TEXT,
  source_file         TEXT,
  confidence          TEXT,
  review_status       TEXT,
  mapping_status      TEXT NOT NULL DEFAULT 'MAPPED',
  exported_at         TIMESTAMPTZ,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (document_id, source_page, row_no)
);
CREATE INDEX IF NOT EXISTS tanda_terima_po_idx   ON tanda_terima (purchase_order_no);
CREATE INDEX IF NOT EXISTS tanda_terima_doc_idx  ON tanda_terima (document_no);
CREATE INDEX IF NOT EXISTS tanda_terima_status_idx ON tanda_terima (mapping_status);

-- ============ Sheet-exact export views (header names in sheet order) ============
CREATE OR REPLACE VIEW v_sheet_faktur_pajak AS
SELECT sor                    AS "SOR",
       billing_number         AS "Billing Number",
       kode_seri              AS "Kode Seri",
       npwp_pengusaha         AS "NPWP & NITKU Pengusaha",
       dasar_pengenaan_pajak  AS "Dasar Pengenaan Pajak",
       ppn                    AS "PPN",
       tanggal_transaksi      AS "Tanggal Transaksi",
       npwp_pembeli           AS "NPWP & NITKU Pembeli",
       nama_pembeli           AS "Nama Pembeli",
       nama_bkp               AS "Nama Barang Kena Pajak",
       qty                    AS "Qty",
       harga_satuan           AS "Harga Satuan",
       jumlah_harga           AS "Jumlah Harga",
       potongan_harga         AS "Potongan Harga",
       uang_muka              AS "Uang Muka",
       ppn_dev                AS "PPN Dev",
       ppnbm                  AS "PpnBM",
       harga_jual_total       AS "Harga Jual Total",
       source_file            AS "Source File"
FROM faktur_pajak;

CREATE OR REPLACE VIEW v_sheet_faktur_penjualan AS
SELECT kode_material          AS "Kode Material",
       sor                    AS "SOR",
       kemasan                AS "Kemasan",
       nama_produk            AS "Nama Produk",
       qty                    AS "Qty",
       harga                  AS "Harga",
       disc_1                 AS "Disc 1",
       disc_2                 AS "Disc 2",
       disc_3                 AS "Disc 3",
       disc_4                 AS "Disc 4",
       disc_5                 AS "Disc 5",
       jumlah                 AS "Jumlah",
       dasar_pengenaan_pajak  AS "Dasar Pengenaan Pajak",
       ppn                    AS "PPN",
       total                  AS "Total",
       source_page            AS "Source Page",
       confidence             AS "Confidence",
       review_status          AS "Review Status"
FROM faktur_penjualan;

CREATE OR REPLACE VIEW v_sheet_po_customer AS
SELECT purchase_order_no      AS "Purchase Order No",
       vendor_code            AS "Vendor Code (SAMB @ client)",
       po_issuer              AS "PO Issuer (Customer)",
       ppn                    AS "PPN",
       product_code           AS "Product Code",
       product_name           AS "Product Name",
       qty                    AS "Qty",
       uon                    AS "UON",
       unit_price             AS "Unit Price",
       discount               AS "Discount",
       total                  AS "Total",
       source_page            AS "Source Page",
       mapping_status         AS "Mapping Status"
FROM po_customer;

CREATE OR REPLACE VIEW v_sheet_tanda_terima AS
SELECT posting_date           AS "Posting Date",
       document_no            AS "Document No",
       purchase_order_no      AS "Purchase Order No",
       vendor_number          AS "Vendor Number",
       item_code              AS "Item Code",
       material_description   AS "Material Description",
       qty                    AS "Qty",
       uon                    AS "UON",
       source_page            AS "Source Page",
       mapping_status         AS "Mapping Status"
FROM tanda_terima;

-- document_id nullable on all four (seed/load rows may predate or span documents)
ALTER TABLE faktur_pajak      ALTER COLUMN document_id DROP NOT NULL IF EXISTS;
ALTER TABLE faktur_penjualan  ALTER COLUMN document_id DROP NOT NULL IF EXISTS;
ALTER TABLE po_customer       ALTER COLUMN document_id DROP NOT NULL IF EXISTS;
ALTER TABLE tanda_terima      ALTER COLUMN document_id DROP NOT NULL IF EXISTS;

-- idempotent natural key per source line (set by load_target_tables.py)
ALTER TABLE faktur_pajak      ADD COLUMN IF NOT EXISTS natural_key TEXT UNIQUE;
ALTER TABLE faktur_penjualan  ADD COLUMN IF NOT EXISTS natural_key TEXT UNIQUE;
ALTER TABLE po_customer       ADD COLUMN IF NOT EXISTS natural_key TEXT UNIQUE;
ALTER TABLE tanda_terima      ADD COLUMN IF NOT EXISTS natural_key TEXT UNIQUE;
