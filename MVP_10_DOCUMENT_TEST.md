# MVP 10-Document Pilot Test

## Test set

Upload ten separate files in one batch or through the form:

1. Faktur Penjualan / invoice
2. Faktur Pajak
3. Sales Order
4. Purchase Order
5. Work Order without price
6. Delivery Order / Surat Jalan
7. Tanda Terima / TTG
8. Kwitansi
9. Bukti Transfer
10. Contract or other non-financial document

Use real business samples where permitted. Redact personal data only in a copy; retain the original checksum relationship in the test log.

## Expected outcomes

| Case | Expected class | Expected route |
|---|---|---|
| Invoice | `invoice` or `faktur_penjualan` | Full mapping / ledger |
| Tax invoice | `faktur_pajak` | Financial mapping, review if tax fields missing |
| SO | `sales_order` | Commercial profile; no fabricated invoice number |
| PO | `purchase_order` | Commercial profile; preserve PO number |
| Work order | `work_order` after classifier update | Operational profile; no forced invoice |
| DO/SJ | `delivery_order` or `surat_jalan` | Item/quantity profile |
| TTG | `tanda_terima` | Receipt/signature profile |
| Receipt | `kwitansi` | Amount/payment profile |
| Transfer | `bukti_transfer` or `bon_transfer` | Payment profile; no automatic AR link without evidence |
| Contract/other | `kontrak` or `lainnya` | Lightweight profile |

## Required checks

- `documents.id`, filename, SHA-256, and original file exist.
- `standard_json.classified.class`, `money_doc`, `confidence`, and `notes` exist.
- Invoice amount uses numeric values and retains `as_written`.
- Handwriting is retained with interpretation and confidence.
- Work order and non-invoice documents retain `doc_class` and do not receive an invented invoice number.
- Low-confidence or missing-key cases are visible for review.
- Re-submit all ten with `INGEST_ALLOW_DUP=0`; all must be `SKIPPED_DUPLICATE`.
- `GET /invoices` and CSV export work without an OCR/model call.
- Secret scan is clean.

## Mixed-PDF / child-document test

Upload one PDF through the intake UI (`/view/upload`) with at least two clearly separated document types, for example:

- pages 1–2: Purchase Order;
- page 3: Delivery Order or Surat Jalan.

Expected behavior:

1. Parent PDF is retained as the audit source.
2. A `document_parts` row is created for each detected page range.
3. A child PDF and child document ID are created for every part.
4. Each child is OCR'd/classified/mapped independently.
5. The PO and delivery note produce separate ledger rows with separate `doc_class` values.
6. A scanned child is OCR'd before classification.
7. An unclear boundary or child goes to review; it is not forced into an invoice.
8. `GET /documents/{parent_id}` returns the child list under `parts`.
9. `others` is checked separately for every child.

Acceptance SQL:

```sql
SELECT parent_document_id, child_document_id, page_start, page_end,
       detected_class, status
FROM document_parts
WHERE parent_document_id = '<parent_id>'
ORDER BY page_start;
```

The splitter is deliberately conservative: a multi-page document with one type remains one child. It currently uses clear document headers/page text; ambiguous packets must be reviewed rather than silently split.

## Pilot evidence to record

- upload batch response;
- final document statuses;
- classifier JSON per document;
- mapped/profile output;
- review reasons;
- elapsed time and model calls;
- duplicate rerun response;
- any manual corrections.
