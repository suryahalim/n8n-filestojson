-- 10-faktur-penjualan-nama-pt.sql — user request 2026-10-02: add customer PT name column to faktur_penjualan
ALTER TABLE faktur_penjualan ADD COLUMN IF NOT EXISTS nama_pt TEXT;
CREATE INDEX IF NOT EXISTS faktur_penjualan_nama_pt_idx ON faktur_penjualan (nama_pt);

-- sheet view: Nama PT placed right after SOR (sheet column order)
-- (PG forbids CREATE OR REPLACE VIEW changing columns -> drop first; view has no dependents)
DROP VIEW IF EXISTS v_sheet_faktur_penjualan;
CREATE VIEW v_sheet_faktur_penjualan AS
SELECT kode_material          AS "Kode Material",
       sor                    AS "SOR",
       nama_pt                AS "Nama PT",
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
