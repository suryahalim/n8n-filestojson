-- 11-faktur-penjualan-page-total.sql — user request 2026-10-04:
-- add real "Page Total" column + tidy records.
-- Background: the parser emitted a synthetic marker row per page footer (nama_produk='[PAGE TOTAL]')
-- holding DPP/PPN/Total; the Total cell sometimes caught OCR junk (page numbers like '2.00').
-- Rules applied here:
--   page_total = printed footer total when plausible (>= DPP and == DPP+PPN within 2%),
--                else DPP+PPN (invoice arithmetic identity — never invented from thin air),
--                else SUM(jumlah) of that page (the page's own item sum, not an estimate).
--   Footer values (dpp, ppn) propagate to every item row of the same page (footer repeats per page).
--   Marker rows WITH item siblings on their page: deleted (data now on the items).
--   Orphan marker rows (footer-only page, e.g. booklet grand total page): KEPT as honest
--   footer records, relabeled with mapping_status='FOOTER-ONLY'.
ALTER TABLE faktur_penjualan ADD COLUMN IF NOT EXISTS page_total NUMERIC(16,2);

-- 1) propagate per-page footers (dpp/ppn + plausible total) onto item rows of the same page
UPDATE faktur_penjualan i
   SET dasar_pengenaan_pajak = COALESCE(i.dasar_pengenaan_pajak, f.dasar_pengenaan_pajak),
       ppn                   = COALESCE(i.ppn, f.ppn),
       page_total            = COALESCE(i.page_total,
                            CASE WHEN f.total IS NOT NULL AND f.dasar_pengenaan_pajak IS NOT NULL
                                      AND f.total >= f.dasar_pengenaan_pajak
                                      AND abs(f.total - (f.dasar_pengenaan_pajak + COALESCE(f.ppn,0)))
                                          <= 0.02 * f.total
                                 THEN f.total END)
  FROM faktur_penjualan f
 WHERE f.nama_produk = '[PAGE TOTAL]'
   AND i.nama_produk <> '[PAGE TOTAL]'
   AND i.document_id = f.document_id
   AND i.source_page = f.source_page;

-- 2) pages whose printed total was junk: identity DPP+PPN
UPDATE faktur_penjualan SET page_total = dasar_pengenaan_pajak + COALESCE(ppn,0)
 WHERE nama_produk <> '[PAGE TOTAL]'
   AND page_total IS NULL
   AND dasar_pengenaan_pajak IS NOT NULL;

-- 3) rows/pages still without any footer: sum of that page's own items
UPDATE faktur_penjualan i
   SET page_total = s.jml
  FROM (SELECT document_id, source_page, sum(jumlah) jml FROM faktur_penjualan
         WHERE nama_produk <> '[PAGE TOTAL]' GROUP BY 1,2) s
 WHERE i.nama_produk <> '[PAGE TOTAL]' AND i.page_total IS NULL
   AND i.document_id = s.document_id AND i.source_page = s.source_page;

-- 4) delete marker rows that have item siblings (their data now lives on the items)
DELETE FROM faktur_penjualan f
 WHERE f.nama_produk = '[PAGE TOTAL]'
   AND EXISTS (SELECT 1 FROM faktur_penjualan i
                WHERE i.document_id = f.document_id AND i.source_page = f.source_page
                  AND i.nama_produk <> '[PAGE TOTAL]');

-- 5) orphan footer rows: keep, relabel honestly (footer-only page = booklet grand total)
UPDATE faktur_penjualan SET nama_produk = NULL, mapping_status = 'FOOTER-ONLY',
       page_total = COALESCE(
         CASE WHEN total >= COALESCE(dasar_pengenaan_pajak,0)
                   AND abs(total - (dasar_pengenaan_pajak + COALESCE(ppn,0))) <= 0.02 * total
              THEN total END,
         dasar_pengenaan_pajak + COALESCE(ppn,0))
 WHERE nama_produk = '[PAGE TOTAL]';

-- sheet view: Page Total after Total
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
       page_total             AS "Page Total",
       source_page            AS "Source Page",
       confidence             AS "Confidence",
       review_status          AS "Review Status"
FROM faktur_penjualan;
