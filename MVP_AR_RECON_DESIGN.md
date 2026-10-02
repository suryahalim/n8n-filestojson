# MVP AR Document Intelligence — Design

Status: MVP design before the 10-document pilot

## 1. Objective

Transform customer documents into searchable, typed, structured records with minimal human review. The MVP must support a 10-document pilot and preserve an enterprise path for 10,000 documents/day.

The MVP does **not** claim to perform SAP/SOR reconciliation yet. It creates the reliable document and relationship foundation required for that phase.

## 2. MVP scope

### In scope

- Batch and form ingestion.
- SHA-256 idempotency and duplicate handling.
- PDF text extraction and image/scanned-document OCR.
- Classification into:
  - `faktur_penjualan` / `invoice`
  - `faktur_pajak`
  - `sales_order`
  - `purchase_order`
  - `work_order`
  - `delivery_order` / `surat_jalan`
  - `tanda_terima`
  - `kwitansi`
  - `bukti_transfer` / `bon_transfer`
  - `credit_note`
  - `kontrak`, `surat_resmi`, `laporan`, `proposal`, `lainnya`
- Per-document confidence, evidence, missing fields, and audit metadata.
- Financial/commercial mapping into the current standard ledger.
- Lightweight identity profile for non-financial documents.
- Human review queue for low confidence or missing identifiers.
- Export/API access for RPA without LLM calls.

### Out of scope for MVP

- Direct SAP write-back.
- Automatic AR settlement posting.
- Guaranteed page-level splitting of a mixed booklet.
- Full customer-specific document generation.
- Autonomous reconciliation when the transfer has no usable reference.

## 3. MVP processing contract

```text
upload
  -> hash/idempotency
  -> MIME/content sniff
  -> text extraction or OCR
  -> classify
  -> route by class + money_doc
  -> structured mapping/profile
  -> review queue if required
  -> persist audit trail
```

Every document must retain:

- original file and SHA-256;
- extracted text/pages;
- classifier JSON and model metadata;
- mapped/profile JSON;
- status transitions and errors;
- source document ID used by downstream RPA.

## 4. Routing rules

- Invoice/faktur: full invoice mapping.
- DO/SJ/TTG/PO/SO/work order: preserve document class; extract identifiers and commercial fields without forcing an invoice number.
- Bukti transfer/kwitansi/CN: preserve payment/credit class and amount; do not invent invoice linkage.
- Contract/letter/report/proposal: lightweight profile; no invoice ledger interpretation.
- `money_doc` is a routing hint, not proof of an invoice. `confidence=low` or missing references must create a review item.

## 5. MVP data model

Current `documents` remains the immutable ingestion/audit record. Current `invoice_rows` is retained as the first structured ledger and gets `doc_class` and `money_doc` for pilot compatibility.

Enterprise follow-up migrations should add:

- `document_pages(document_id, page_no, text, ocr_engine, confidence)`
- `document_entities(document_id, entity_type, value, source, confidence)`
- `document_links(source_document_id, target_document_id, link_type, confidence, method)`
- `ar_items(external_ar_id, customer, currency, amount, open_amount, status)`
- `payment_proofs(document_id, bank_date, payer, amount, reference, account)`
- `reconciliation_matches(payment_id, ar_id, allocated_amount, confidence, state)`
- `review_queue(document_id, reason, priority, claimed_by, resolution)`

No runtime DDL. Every change must be a numbered `db-init/*.sql` migration and be applied explicitly to existing volumes.

## 6. Enterprise scale path

For 10,000 documents/day, the current background-thread OCR must evolve into queue-backed workers:

```text
API -> ingest queue -> splitter workers -> OCR workers
       -> classifier workers -> mapper workers -> reconciliation workers
       -> review queue / downstream SAP adapter
```

Required controls:

- bounded concurrency per provider/model;
- retry with exponential backoff and dead-letter queue;
- idempotency key on every stage;
- per-document and per-page metrics;
- provider/model version recorded on output;
- encrypted object storage for originals;
- role-based access and audit log;
- retention and deletion policy;
- load tests at 100, 1,000, and 10,000 documents/day.

## 7. Pilot acceptance criteria

The 10-document pilot passes when:

- all 10 originals are stored and uniquely identified;
- duplicate re-upload does not create a second record in production mode;
- every document receives a class, confidence, and classifier evidence;
- invoice fields are numeric/ISO-normalized where evidence exists;
- work orders do not become fabricated invoices;
- ambiguous documents enter review with a reason;
- all outputs can be exported without another LLM call;
- no API key appears in payloads, database output, Git, or backup JSON.
