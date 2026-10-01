# Doc Pipeline

IT document ingestion → extraction → validation → OCR → service-bus delivery, orchestrated with n8n.

**Flows:**
- **Ingest:** Upload (n8n Form multi-file, or API) → store original + job (Postgres) → extract (pypdf/openpyxl, content-sniffing MIME) → standard JSON → validate.
- **Pass:** publish to RabbitMQ (`job.deliver`) → worker POSTs JSON to receiving API with retry/backoff/sweeper → `DELIVERED`.
- **Flag:** empty/short text (scans, photos) → `FLAGGED` → auto-OCR via n8n (Tesseract or Qwen) or human fix → revalidate → deliver.
- **Dedupe:** sha256 skip on batch path by default (`SKIPPED_DUPLICATE`); bypass with `INGEST_ALLOW_DUP=1` (currently ON for testing).

## Architecture

```
                    ┌───────────────────────────── n8n (:5678) ─────────────────────────────┐
  Form ────────────▶│ docpipeline-form-upload ──┐                                           │
  /form/it-upload   │  (multi-file → batch API) │        docpipeline-review-01              │
                    │                           │  (review-flagged webhook → correct path)  │
  curl/API ────────▶│                           │        docpipeline-ocr-01                 │
  :5000/documents   │                           │  (webhook /webhook/ocr-scan → engine? →   │
                    └───────────────────────────┼──────────────────────────────────────────┼┘
                                                ▼                                           │
        ┌───────────────────── Extractor API :5000 (FastAPI) ─────────────────────┐         │
        │ /documents /documents/batch          ingest+sha256 dedupe (toggleable)  │◀────────┘
        │ /documents/{id}/ocr?engine=qwen|tesseract   ← holds QWEN_API_KEY+MODEL  │
        │ /documents/{id}/correct  /documents/{id}/track  /documents/{id}/raw     │
        │ /view  /view/<id>  /view/inbox   /mock/receiving                        │
        │ local: pypdf · openpyxl · pdftoppm · tesseract │ remote: Qwen vision API │
        └──────┬───────────────────────────────┬──────────────────────────────────┘
               ▼                               ▼
        Postgres (dp-db)                 RabbitMQ (doc_pipeline → job.deliver)
        documents/jobs/delivery_tasks           │ worker (backoff + sweeper)
               audit trail: document_events     ▼
                                        Receiving API (TARGET_API_URL — mock until real set)
                                        → DELIVERED / retry / dead
```

**Key design rule:** API keys, model, and endpoint live ONLY in the extractor (runtime file `data/ocr_config.json`, editable at `/view/settings`, hot-reloaded; env vars as bootstrap). n8n never holds or forwards secrets — its OCR workflow only routes `document_id` + engine choice to extractor endpoints. Swapping OCR model/provider = save in the UI, effective instantly, zero restarts and zero workflow edits.

**FastAPI docs:** `http://<host>:5000/docs` (Swagger UI) · `http://<host>:5000/redoc` (ReDoc) · `http://<host>:5000/openapi.json` (OpenAPI). The current extractor API has no request-token auth on the protected Tailscale network; do not expose port 5000 publicly without adding auth. Credential setup and the redacted test checklist are in `TESTING.md` §0.

## Configuration (single source: `~/doc-pipeline/.env`, gitignored, mode 600)

**Current operating mode (OCR-only):** upload is automatically processed page by page. The output is `standard_json.pages[]` (one `{page,text}` object per page) plus raw OCR text; classification, splitting, mapping, invoice tables, and delivery are disabled by default with `MAP_AUTO=0`. Monitor with `GET /documents/{id}/progress`; retrieve the complete JSON/raw text with `GET /documents/{id}`. `OCR_MAX_PAGES=0` means every page.

**Auto-chain OCR (since v1.4):** uploading via the n8n form is now end-to-end — flagged documents get OCR'd automatically right after intake, sequentially in a background thread. The form answers immediately; poll the progress endpoint or open `/view/<id>` while processing.

**Primary path since 2026-09-20: the Settings UI** — `http://<host>:5000/view/settings` edits endpoint URL / API key / model / default engine, saved to `data/ocr_config.json`, **hot-reloaded on the next request — no restart, no compose edit, no n8n change**. Saves are gated by a live test-connection (tiny vision call): wrong key → "key rejected (401)", wrong model name → "model not available in this plan (404)" (this is how qwen-vl-ocr's absence was caught), unreachable URL → timeout hint. `Force save` bypasses the gate. Key is never returned by the API (masked only: prefix…suffix + sha8). Optional PIN gate: set `SETTINGS_PIN` in `.env`. Design: `DESIGN_OCR_SETTINGS.md`.

Env vars below are the **bootstrap/DR path** (used only until the UI saves a config; delete `data/ocr_config.json` to fall back):

| Var | Default / current | Meaning |
|---|---|---|
| `PIPELINE_DB_PASSWORD` | set | Postgres password |
| `RABBITMQ_USER` / `RABBITMQ_PASS` | set | broker creds |
| `N8N_ENCRYPTION_KEY` | set | n8n credential encryption |
| `N8N_OWNER_EMAIL` / `N8N_OWNER_PASS` | set | first-run owner claim |
| `N8N_SECURE_COOKIE` | `false` | required on plain HTTP/Tailscale; remove when HTTPS |
| `QWEN_API_KEY` | set | Token Plan vision key (extractor only) |
| `QWEN_MODEL` | `qwen3.8-flash` | OCR model — swap e.g. `qwen-vl-ocr`, no code change |
| `QWEN_URL` | token-plan compatible-mode | chat-completions endpoint |
| `OCR_ENGINE` | `qwen` | fallback engine when caller omits `engine` (`qwen`\|`tesseract`) |
| `OCR_WORKERS` | `4` | parallel OCR workers (page-level queue) |
| `OCR_NUMERIC_VERIFY` | `0` | selective visual re-check of numeric pages; OFF by user decision 2026-09-24 (rows that fail arithmetic then stay `REVIEW-ARITH`, values kept verbatim) |
| `INGEST_ALLOW_DUP` | `1` (testing) | `0` = sha256 dedupe ON for production |
| `OCR_MAX_PAGES` / `OCR_DPI` | `0` / `200` | `0` = process every page; positive value = optional OCR cap / render DPI |
| `TARGET_API_URL` | mock | real receiving endpoint; contract `{"accepted":true}` |

Change any of these → `cd ~/doc-pipeline && docker compose up -d extractor` (env vars are read at container start).

## OCR — engines & usage

| Engine | Cost | Speed | Strength | Watch-out |
|---|---|---|---|---|
| `qwen` (qwen3.8-flash) | API tokens | ~20 s/page photo, ~22 s/page scan | photos, handwriting, low contrast, **exact digits** | paid per page; 19 pgs ≈ 37K in + 8K out tokens |
| `tesseract` (local, eng+ind) | free | ~2.5 s/page | clean flatbed scans | digit confusion 0↔6, fails on green carbon-copy photos |

Trigger (per document, after it shows FLAGGED):
```bash
curl -X POST http://<host>:5678/webhook/ocr-scan -H 'Content-Type: application/json' \
  -d '{"document_id":"<id>","engine":"qwen"}'        # engine omit → OCR_ENGINE default (qwen)
```
n8n UI equivalent: workflow *DocPipeline - OCR Scan (Tesseract | Qwen)* → Execute → set body. Re-OCR **replaces** previous engine text (never merges). Full scenarios: `OCR_TEST_PLAYBOOK.md` (T1–T8).

## Quick start (fresh machine)

```bash
git clone git@github.com:suryahalim/n8n-filestojson.git doc-pipeline && cd doc-pipeline
cp .env.example .env && chmod 600 .env      # fill real values incl. QWEN_API_KEY
docker compose up -d --build
# claim n8n owner at :5678, then import ALL three workflows:
for f in workflow-form-upload workflow-review-loop workflow-ocr-scan; do
  docker cp n8n/$f.json dp-n8n:/tmp/wf.json
  docker exec dp-n8n n8n import:workflow --input=/tmp/wf.json
done
# ⚠ import:workflow creates/updates the definition but does NOT activate/publish:
docker exec dp-n8n n8n update:workflow --id=docpipeline-form-upload  --active=true
docker exec dp-n8n n8n update:workflow --id=docpipeline-review-01    --active=true
docker exec dp-n8n n8n update:workflow --id=docpipeline-ocr-01       --active=true
docker exec dp-n8n n8n publish:workflow  --id=docpipeline-form-upload
docker exec dp-n8n n8n publish:workflow  --id=docpipeline-review-01
docker exec dp-n8n n8n publish:workflow  --id=docpipeline-ocr-01
docker restart dp-n8n                        # webhook routes refresh after publish
python3 tests/test_api_e2e.py               # green stack proof
```
**Note:** `.env` is NOT in the repo by design (secrets never committed — verified by full-history scan). Restore it from the current server's copy.

## Standard invoice ledger (RPA contract)

Every OCR'd document with `doc_type=invoice` is normalized into the **`invoice_rows`** table (Postgres `pipeline`) — one row per document, safe to re-extract (upsert; review columns preserved).

- Columns: extraction result (vendor*, invoice_number + source/confidence, invoice_date, ref_po, subtotal/tax/total NUMERIC, total_as_written, line_items/handwritten JSONB, **others** free text for important unmapped details, confidence, missing, notes, mapper_model) + RPA write-back columns (`rpa_vendor, rpa_invoice_number, rpa_date, rpa_total, rpa_note, rpa_status, rpa_reviewed_at`) + workflow `status`: `extracted → pending_review → mapped`.
- RPA endpoints: `GET /invoices?status=extracted` (pull queue) · `PATCH /invoices/{document_id}` (claim / write final values) · `GET /invoices/export.csv` (includes `others`). RPA calls are pure Postgres — **zero LLM tokens**; only OCR and the mapping step consume the model.
- **Schema policy (decided 2026-09-21):** schema lives ONLY in `db-init/*.sql` migrations — no DDL in app code. Fresh machine: postgres applies all db-init files automatically. **Existing DB created before a migration: apply it once manually**, e.g. `docker exec -i dp-db psql -U pipeline -d pipeline < db-init/04-add-others.sql` and `05-document-parts.sql`. Same rule for any future table change.

### Multi-document PDF splitting

When an uploaded `other` PDF contains distinct document headers across pages, the extractor conservatively detects page boundaries, keeps the original as the parent document, creates child PDFs, and routes each child independently through OCR/classification/mapping. Child results are linked in `document_parts` and exposed by `GET /documents/{id}` under `parts`. A child that is unclear remains in review; the system does not force one mixed packet into one invoice.

Schema is in `db-init/05-document-parts.sql`. Detection is intentionally conservative: multi-page documents of one type remain one child; scanned pages are OCR'd before splitting.


**Invoice → structured table mapping (v1.5):** OCR'd documents whose `doc_type` is `invoice` get an extra auto step: `map_std_to_table()` sends the extracted text to the same LLM endpoint (config from Settings UI) with an Indonesian invoice-extraction prompt and stores the result in `standard_json.mapped` — normalized Rupiah (`Rp 1.234.567,89` → `1234567.89`), invoice number + `number_source` (printed/handwritten/stamp/inferred) + confidence, dates ISO, line items, handwritten transcriptions with interpretations, `missing[]` and `notes`. `/view/<id>` renders it as tables; `GET /documents/{id}/mapped.csv` = one CSV row for the vendor ledger; delivered payload includes `mapped` so downstream/n8n can consume JSON directly. Manual: `POST /documents/{id}/map {"doc_type":"invoice"}`. Knobs: `MAP_AUTO=0` disables auto step, `MAP_TIMEOUT` (default 600s). Multi-page cost control: >3 pages → full text of pages 1-2 + last + 300-char digests of the middle (a raw 28K-char 33-page doc exceeded 300s model generation; the digest version finished in ~197s).
NOTE (honest limits, verified 2026-09-21): a 33-page multi-SO sales-booklet returned total `0,00` from page 1's printed FAKTUR grand total while page 2 held a different PO total, and left `invoice.number` null because none was explicitly labelled — exactly the behaviour designed for (no fabrication; check `missing[]`/`notes`). Single invoices/photos map cleanly (DO photo: 8 handwritten entries incl. plate + paraf interpreted). For very long mixed docs, consider mapping per real invoice page or raising OCR text quality.

**Change OCR model:** open `http://<host>:5000/view/settings` → edit Model (or Fetch models) → Test → Save. Effective immediately — no restart. CLI fallback still works: edit `QWEN_MODEL` in `.env` + `docker compose up -d extractor` (only read when `data/ocr_config.json` absent). Verify model exists in plan first (404 = not available).

## Batch mapping to Google Sheets (RPA result tables) — `scripts/batch_map.py`

The large-scale path proven on 1.276 real documents (1.257 e-Faktur PDFs + 19 scan booklets, ~12K OCR pages): map everything **deterministically, zero LLM tokens**, into a fresh copy of the `Result RPA` template.

```bash
cd ~/doc-pipeline
python3 scripts/batch_map.py --dump /tmp/dump.json \
  --window '2026-09-24T12:00|2026-09-24T12:20|efaktur' \
  --window '2026-09-24T13:00|2026-09-26T23:59|scans' <SPREADSHEET_ID>
```

- Pulls `standard_json` per doc from the extractor API (window-based, cached in `/tmp/batch_map_docs.json`; refresh per-document after OCR completes).
- Routes by type: e-Faktur → 3-layout header+item parser (incl. PPN-dibebaskan); scan booklets → per-vendor page classifier → Faktur Penjualan / PO Customer / Tanda Terima / Surat Jalan tabs.
- **PO Customer columns (schema 2026-10-01, rules engine po_v2.py):** `Purchase Order No | Vendor Code (SAMB @ client) | PO Issuer (Customer) | PPN | Product Code | Product Name | Qty | UON | Unit Price | Discount | Total | Source Page | Mapping Status` — 13 cols. Rules (knowledge/po_rules.json): PPN only `11%`/`1.1%`; Vendor Code = SAMB's code in the client's eyes (never a name); PO Issuer = SAMB's customer, always `PT ...` (proven brand→PT aliases in knowledge/issuers.json, else printed store name tagged `|ISSUER-STORENAME`); Total = Qty×UnitPrice−Discount, arithmetic-gated (0 violations on MAPPED rows); EVERY row carries a Product Name — nameless stubs dropped (raw pages stay in OCR Mapping Review tab). Parser chain: per-vendor layout parsers (Hypermart E-PO, Lion, GrandLucky, HariHari, AEON-slip321, DutaBuah, Gramedia, Kage, Berkah, Astro, Midi-LPB, TiPTop...) + generic arithmetic-gated tokenizer (item_fallback.py). Self-learn: issuer aliases proven by page co-occurrence (≥5 events, consistent) are written to issuers.json, never guessed.
- **Never invents values.** Every row carries provenance `p<page> <filename>`; unparseable pages land in `OCR Mapping Review` with raw text, not in data tabs. Arithmetic gate (`qty×harga=jumlah`, Σitem=footer) labels rows `PASS` / `REVIEW-ARITH` (`OCR_NUMERIC_VERIFY=0` keeps values verbatim without visual re-check).
- Tidy filter: placeholder/header-only rows are never written (user rule: "if it doesn't make sense, don't include rows at all").
- Writes to the **copy** only; template untouched; sheet id kept in `copy_sid.txt`.

**Self-learning vendor rules** — `scripts/vendor_rules.json`: `self_companies` (PT Sarana Abadi Makmur Bersama is OUR company, never a vendor), `junk` patterns (OCR label bleed like `PT ORDER DATE`), `learned_po_vendor` memory (PO→vendor, grows every run), `canon_vendor()` name normalization.

**Self-learning variant KB (llm-wiki pattern)** — `scripts/knowledge.py` + `knowledge/`: unit of knowledge = layout variant (item-table column signature × company). Each run *applies* promoted semantics before writing (relabels `REVIEW-ARITH → PASS-LEARNED` only on exact arithmetic proof; OCR digit-confusion price fixes only when the rule was seen ≥2× in the same variant and the corrected value re-fits exactly) and *learns* after a successful write. `knowledge/companies/*.md` is the human-readable wiki (evidence, promotions, contradictions); `knowledge/INDEX.md` lists companies. Promoted today: `qty=carton*isi+pcs` for the SAMB sales booklet (1.988 proven rows). Nothing is ever corrected without an exact arithmetic proof.

**Verification** — `scripts/read_sheet.py` (live read-back → `/tmp/sheet_now.json`) + `scripts/verify_sheet.py` (write integrity, Σitem=footer per faktur, PASS-LEARNED re-proof, provenance within real page counts, vendor rules, empty-row sweep, window coverage). Last full run 2026-09-30: **0 real problems**; snapshots committed under `snapshots/` for manual review. TT quirks fixed: 67 header-bleed dates repaired from raw page text, 314 date formats normalized.

**Restore dedupe after testing:** `INGEST_ALLOW_DUP=0` in `.env` → `docker compose up -d extractor` → same batch upload must show `SKIPPED_DUPLICATE`; run `python3 tests/test_batch_e2e.py`.

**Edit a workflow (the n8n trap — learned the hard way):** production runs the **published** version, not the one you edit. After any change: Publish button top-right, or the CLI sequence above + `docker restart dp-n8n`. A webhook returning stale behavior = almost always unpublish, not code.

**Rebuild extractor after code change:** `docker compose build extractor && docker compose up -d extractor` (apt layer cache can be GC'd — first rebuild may take ~10 min; normal).

**Health / logs / state:**
```bash
curl http://<host>:5000/health                                   # {"ok":true,"db":true}
docker logs dp-extractor --since 10m | tail -20                  # upload/OCR requests
docker logs dp-worker --since 10m | tail                         # delivery retries
docker exec dp-db psql -U pipeline -d pipeline -Atc \
  "SELECT id,status,length(standard_json::text) FROM documents ORDER BY created_at DESC LIMIT 10;"
# n8n executions (error forensics):
docker exec dp-db psql -U pipeline -d n8n -Atc "SELECT id,status FROM execution_entity ORDER BY id DESC LIMIT 5;"
```

**House rules:** never commit `.env`, keys, or DB dumps (`backups/` is gitignored) — pre-push history scans enforced; bind host IPs in `docker-compose.yml` (this box exposes on localhost + Tailscale IP only).

## Pointers
- Operations & UI walkthrough: `USAGE.md` · UI test suite (U-series) + batch mapping suite (M-series §9): `TESTING.md` · OCR-focused playbook (T1–T8): `OCR_TEST_PLAYBOOK.md`
- MVP AR design: `MVP_AR_RECON_DESIGN.md` · Ten-document pilot and mixed-PDF child test: `MVP_10_DOCUMENT_TEST.md` · Mixed-PDF live test: `TESTING.md` U13
- Receiving contract: `TESTING.md` §4 · Real endpoint switch: set `TARGET_API_URL`
- n8n workflows source of truth: `n8n/*.json` (keep committed copies in sync with live DB after every publish)
