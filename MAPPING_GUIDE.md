# Mapping Guide — upload → OCR → 4 DB tables (live since 2026-10-02)

Destination of record is **Postgres** (`dp-db`, schema `pipeline`), not Google Sheets.
Sheets export remains optional (`scripts/map_sheets.py`, historical path).

## 1. Where the results are

| What | Where |
|---|---|
| Grid per tab + filter + CSV download | `http://<host>:5000/view/tables` |
| Mapping monitor (per-folder progress, needs-attention) | `http://<host>:5000/view/mapping` |
| Rows JSON | `GET /mapping/rows?tab=po_customer&folder=%X%` |
| Sheet-exact CSV | `GET /mapping/export.csv?tab=faktur_penjualan&folder=%X%` |
| Queue state | `GET /mapping/status` |
| Direct SQL | `docker exec -it dp-db psql -U pipeline -d pipeline` → `faktur_pajak`, `faktur_penjualan`, `po_customer`, `tanda_terima` |

Tables: schema in `db-init/08-target-mapping-tables.sql` (+ `db-init/09-map-tasks.sql` queue,
`db-init/10-faktur-penjualan-nama-pt.sql` adds `nama_pt`,
`db-init/11-faktur-penjualan-page-total.sql` adds `page_total` + tidy rules below).
Column order mirrors the RPA sheet tabs; `v_sheet_*` views + `/mapping/export.csv` give sheet-exact
headers. Idempotency: `natural_key UNIQUE` upsert; re-mapping a document DELETEs its rows first.

### faktur_penjualan tidy semantics (2026-10-04, `_tidy_fp_rows` in app.py, both writers)
- The parser's synthetic footer marker row (`nama_produk='[PAGE TOTAL]'`) is NOT data: its
  DPP/PPN/total are spread onto every item row of the same page, then the marker is dropped.
- `page_total` = printed footer grand total, accepted only when plausible (`>= DPP` and
  `== DPP+PPN` within 2%) — OCR junk in the TOTAL cell (page numbers like `2.00`) fails this;
  fallback = `DPP+PPN` identity, else the page's own `SUM(jumlah)`.
- Footer-only pages (booklet grand-total page with no item rows) stay as ONE honest row,
  `mapping_status='FOOTER-ONLY'`.
- API engine prompt instructs qwen to set `page_total` per item row and never emit marker rows.
- Verified 2026-10-04: 90 rows, page_total 100% filled, 0 markers, 0 pages off DPP+PPN identity.

## 2. The chain (all automatic)

```
upload (UI /view/upload | POST /documents/folder[/scan])
  → ingest + sha256 dedupe → FLAGGED → auto-OCR (OCR_AUTO=1, OCR_WORKERS=4)
  → finalize_status=VALIDATED  ⇒  _map_enqueue(document_id)      [queue of record: map_tasks]
  → engine maps → POST rows → SERVER GATE → target table → task=mapped
```

Enqueue happens on every path (single, batch, folder, async multi-page OCR completion —
the `finalize_status` hook, commit `556a6bf`, closed the async gap where 70 docs missed the queue).

### Engines
- **`hermes` (deterministic, zero tokens)** — `scripts/map_engine.py`; parsers map_sheets/po_v2/
  batch_map/efaktur. Run one pass: `python3 scripts/map_engine.py --limit 50`.
  Daemon watch mode: `python3 scripts/map_engine.py --watch --limit 50` (polls `/mapping/pending`
  every 20 s). NOTE: currently started as a child of the Hermes session for testing — dies on
  session/gateway restart; harden to a compose service (`dp-mapper`) for production.
- **`api` (LLM: qwen3.8-flash via Token Plan — same endpoint/key as OCR)** — for layout variants
  the parser loses (wrapped 2-line product names, stamped POs, Surat Pesanan, Venditore/Kino
  supply sheets…). Config: `/view/settings` → Mapping AI → saved to `data/map_config.json`
  (hot-reload). Prompt = knowledge rules digest (`/mapping/rules`) + target columns + OCR text
  (24k cap). Output still passes the SAME server gate before rows land.
- **Batch drain of `review`/`failed`**: `python3 scripts/drain_review.py 4 45` — 4 workers,
  each atomically `POST /mapping/claim` one doc then `POST /mapping/run`. Throughput: 44 docs / 2.3 min.

### Mandatory gates (`/mapping/rows` server-side, rules `knowledge/po_rules.json`)
- `product_name` required on every PO row; `po_issuer` must be a proven PT (never SAMB).
- Arithmetic `Total = Qty × UnitPrice − Discount` (2% tol; gross-of-PPN accepted, honest REVIEW-ARITH on fail).
- PPN ∈ {11%, 1.1%}; numeric clamp (>1e12 → NULL); never invents — unreadable stays blank/REVIEW.
- **Provenance fallbacks** (server fills when the document body omits it, never guesses content):
  - `po_issuer` from bundle folder name `NN 110000XXXX - PT NAME - SOR…` → status `MAPPED-FOLDER-ISSUER`
  - `faktur_penjualan.nama_pt` (customer PT) — LLM reads letterhead; folder provenance fills the rest.
- `POST /documents/{id}/mapping/rows` REFUSES an empty rows array (422) — previously an empty
  payload wiped the document's rows before failing.

## 3. Performance facts (measured, 2026-10-02)

- qwen3.8-flash hides reasoning tokens by default: ~90 % of the completion (5822/6356) →
  90–400 s per mapping call. **Always send `"enable_thinking": false`** → ±13 s/doc, identical rows.
- The mapping HTTP handlers run the LLM call in a worker thread (`asyncio.to_thread`);
  blocking it inside `async def` deadlocks the event loop under concurrency (that's what made
  the first "4 workers" drain serial).
- Full-bundle reference (94 docs, 24 customers): OCR ±19 min (4 workers); deterministic mapping
  58/94 inline; LLM drain of 36 + remediation cycles ±10 min; end state **94/94 mapped**,
  120 po rows incl. AEON 8 items, 0 empty issuers, `nama_pt` 110/110.
- Scale estimate for ~2k docs: OCR ±6 h at 4 workers (±3.5 h at 8 — env `OCR_WORKERS`),
  mapping tail (LLM drain) ±1 h at 4 workers → **±7–8 h (4w) / ±4 h (8w)**. New layout families
  land in `review` first pass and get drained in the same remediation loop.

## 4. Audit — prove every folder got mapped

```bash
python3 scripts/audit_folders.py
# A: map_tasks status totals    B: per-folder rows in each of the 4 tables
# C: anomalies: non-mapped tasks, mapped-but-zero-rows, empty po_issuer
```
User rule: **every uploaded document must end MAPPED** — a review state is a mapper bug to fix,
not something to hand back. Remediation: set the task back to `review` (or run `drain_review.py`
which reads `?statuses=review,failed`), the API engine re-maps with the rules digest.

## 5. Gotchas (each one cost a real incident)

- `ORDER BY t.created_at` (not `created`); sheet rows must be dicts (column name → value), not lists.
- Async multi-page OCR finalizes AFTER the folder worker returns → enqueue is done in `finalize_status`,
  don't rely on the caller-side hook only.
- `/mapping/pending?statuses=review,failed` — the hermes watcher consumes `pending`; API drain
  consumes `review,failed`; atomic claim prevents double-processing when both run.
- urlopen timeout for `api` calls: 900 s (reasoning models were 400 s+ before the thinking fix).
- Backfill enqueue folder filter is LIKE on the FULL stored path: use `%Complete bundles%`, not `Complete bundles%`.
- Postgres forbids `CREATE OR REPLACE VIEW` changing columns → drop + recreate (db-init/10).
- Migration files: SQL comments use `--`, never `#`.
- Scanner/secret hygiene: never paste API keys/`.env` values into commands or docs;
  map_config key = same QWEN token plan key as OCR.
