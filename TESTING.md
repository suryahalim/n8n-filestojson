# Doc Pipeline — Test Case Suite & Documentation

Version: 2026-09-30 · Base cases executed live 2026-09-18 — **PASS** (U10 is the composite of U1–U4+U7, each verified live that day; run it as one sitting during real UAT).
**OCR testing (2026-09-19): use `OCR_TEST_PLAYBOOK.md` — T1–T8, dedupe currently OFF for testing (`INGEST_ALLOW_DUP=1`).**
**Batch mapping testing (2026-09-24→30): see §9 M-series — full-run evidence on 1.276 documents, live verification 2026-09-30: 0 real problems.**

**Rule of thumb:** UI cases (U-series) = what your staff/stakeholders will do. Code cases (C-series) = regression checks you (or CI) run once before declaring any change safe. Mapping cases (M-series) = read-back proofs against the live Google Sheet, run after every `batch_map.py` write.

---

## 0. Entry points

| Surface | URL | Purpose |
|---|---|---|
| Upload Form | http://100.68.212.36:5678/form/it-upload | intake, **multi-file batch**, no login |
| Result dashboard | http://100.68.212.36:5000/view | per-document status, parsed result, delivered payload |
| Receiving inbox | http://100.68.212.36:5000/view/inbox | last 20 payloads the receiving API actually got |
| API console (Swagger) | http://100.68.212.36:5000/docs | every endpoint, click "Try it out" |
| n8n | http://100.68.212.36:5678 | Executions = audit of each form/webhook run |
| RabbitMQ | http://100.68.212.36:15672 (`pipeline` / `.env`) | watch queue `job.deliver` drain |

### FastAPI documentation and credentials

The extractor is a FastAPI service. Its generated API documentation is available at:

```text
Swagger UI:  http://100.68.212.36:5000/docs
ReDoc:       http://100.68.212.36:5000/redoc
OpenAPI JSON: http://100.68.212.36:5000/openapi.json
Health:      http://100.68.212.36:5000/health
```

The current extractor endpoints do not require an API token when accessed on the protected Tailscale network. Do not expose port 5000 publicly without adding an auth layer.

Credential checklist for a fresh test environment:

| Credential/config | Needed for | Where it belongs | GitHub policy |
|---|---|---|---|
| `PIPELINE_DB_PASSWORD` | Postgres, n8n DB, extractor DB | local `.env` | placeholder only |
| `RABBITMQ_USER` / `RABBITMQ_PASS` | RabbitMQ UI and worker | local `.env` | placeholder only |
| `N8N_OWNER_EMAIL` / `N8N_OWNER_PASS` | n8n editor login | local `.env` | placeholder only |
| `N8N_ENCRYPTION_KEY` | n8n credential/database encryption | local `.env` | placeholder only |
| `QWEN_API_KEY` | Qwen OCR/classification/mapping | extractor `.env` or `/view/settings` | never commit |
| `SETTINGS_PIN` | optional protection for OCR Settings UI | local `.env` | placeholder only |

Non-secret test values are documented in `.env.example`. Copy it to `.env`, replace every `change-me` value locally, and use `chmod 600 .env`. The runtime OCR key may also be stored in `data/ocr_config.json`, which is gitignored and must remain local.

Never put real passwords, API keys, OAuth tokens, encryption keys, or connection strings in `TESTING.md`, `README.md`, workflow JSON, issues, or commits. Use `[REDACTED]` or `change-me` in shared documentation. Existing live credentials are intentionally not reproduced here.

Credential requirements by test:

- U0/U1/U2/U3/U4/U7/U8/U13 through the form/API: no credential in the request; network access to the host is required.
- n8n editor/execution inspection: `N8N_OWNER_EMAIL` + `N8N_OWNER_PASS`.
- RabbitMQ inspection: `RABBITMQ_USER` + `RABBITMQ_PASS`.
- Qwen OCR tests: `QWEN_API_KEY`, unless using local Tesseract only.
- Direct PostgreSQL verification: `PIPELINE_DB_PASSWORD` and local Docker access.
- `tests/test_api_e2e.py` and `tests/test_batch_e2e.py`: local Docker stack and fixtures; they read the configured service endpoints and do not need a credential in the test command.

## 1. Fixture pack (the "PowerStore PO" story)

| File | Kind | Used in |
|---|---|---|
| `PO-2026-8412.pdf` | 2-page PO, text tables + checklist | U1, U7, C1 |
| `SCAN-delivery-note-DN-991.pdf` | scan, **no text layer** | U2 |
| `SCAN-photo-DN-991.jpg` | photo of delivery note | U3, U4, U7 |
| `IT-Asset-Register-PO-8412.xlsx` | 3 sheets: Assets / Warranty / MaintenanceLog | U6, U7, C1 |

On server: `~/doc-pipeline/tests/fixtures/`. Files also shared in chat (Sept-18).

---

## 2. UI test cases (browser only — phone OK)

### U0 · Health — PASS ✅
Open all six URLs above; Swagger `GET /health` → `{"ok":true,"db":true}`.

### U1 · Happy path — valid PDF → DELIVERED — PASS ✅
Form → upload `PO-2026-8412.pdf`, type *Report*, notes `ui test` → Upload.
Expect result page: `VALIDATED` + Document ID. Within ~5 s: `/view/<id>` → badge **DELIVERED**, page text contains `PO-2026-8412`, checklist table, `1,347,873,000`; "Delivered payload" JSON shows the parsed text.

### U2 · Flagged — scanned PDF (no text layer) — PASS ✅
Form → upload `SCAN-delivery-note-DN-991.pdf`.
Expect result page **FLAGGED**; `/view/<id>` lists flag rule `min 20 chars (scanned/blank pdf? needs OCR)`; no delivery task yet.

### U3 · Flagged — photo — PASS ✅
Form → upload `SCAN-photo-DN-991.jpg`. Expect FLAGGED `image requires OCR result`.

### U4 · Review loop — fix a flagged doc → DELIVERED — PASS ✅
Take U3's Document ID. Swagger → `POST /documents/{id}/correct` → body:
```json
{"extracted": {"kind": "image", "ocr": "DELIVERY NOTE DN-991, PowerStore 300T SN PST300T-77412 received at DC Cileungsi", "bytes": 46081}}
```
Execute → `{"validated": true, "delivery_task": "..."}`. Reload `/view/<id>` → **DELIVERED**, OCR section shows the text. (n8n-automated variant: same JSON + `"document_id"` POSTed flat to `http://100.68.212.36:5678/webhook/review-complete` → `{"routed":true,"validated":true}` — PASS ✅.)

### U5 · Retry — downstream failure self-heals — PASS ✅
Swagger → `POST /mock/receiving/fail-next` → Execute. Then Form-upload any valid PDF.
Expect `/view/<id>`: task `RETRY_WAIT attempts 1` (~7 s) → **auto `DELIVERED`** after 30–40 s, zero manual action.

### U6 · Excel structured rows — PASS ✅
Form → upload `IT-Asset-Register-PO-8412.xlsx`. Expect DELIVERED and `/view/<id>` renders **three HTML tables** (Assets 6 rows, Warranty 4, MaintenanceLog 5); delivered payload carries columns+rows JSON (not flat text).

### U7 · Batch upload (multi-file) — PASS ✅
Form → select `PO-2026-8412.pdf` + `IT-Asset-Register-PO-8412.xlsx` + `SCAN-photo-DN-991.jpg` together (Ctrl / long-press) → type *Report* → Upload.
Expect ONE result page: `Batch result — 3 file(s)` + per-file lines, e.g. `VALIDATED:2 FLAGGED:1` with each file's document ID (fresh copies) or `SKIPPED_DUPLICATE` (if the file was uploaded before). `/view` shows all new rows.

### U8 · Dedupe (upload the same file twice) — PASS ✅
Upload `PO-2026-8412.pdf`, note its ID. Upload the identical file again.
Expect second submission: `SKIPPED_DUPLICATE` + **the existing** document ID — nothing stored/delivered twice.

### U9 · Audit trail — PASS ✅
n8n → **Executions**: every form submission (single and batch) = one row with green status; open it → Prepare Batch node shows file names; extractor response shows per-file results. RabbitMQ console → `job.deliver`: publish spike per validated file, drains to Ready=0 instantly (1 consumer).

### U10 · Staff simulation — acceptance run (10 min, phone only)
U1 → U2 → U3 → U4 → U7 in one sitting, only Form + /view + Swagger. Green = the system is usable by IT staff with no terminal.

### U13 · Mixed PDF → child documents — PASS ✅
Create/upload one packet PDF containing the 2-page `PO-2026-8412.pdf` followed by the first page of `SCAN-delivery-note-DN-991.pdf`, with form type `Other`.

Expected sequence:

1. Packet is `FLAGGED` because one page has no text layer.
2. Auto-OCR runs before splitting.
3. Parent becomes `document_scope=multiple`.
4. `document_parts` contains page ranges `1–2` and `3–3`.
5. Child 1 is classified `purchase_order`, mapped, and has total `1347873000`.
6. Child 2 is classified `delivery_order`, mapped independently, and does not inherit the PO total.
7. Both child rows have their own `others` value where applicable.
8. `GET /documents/{parent_id}` exposes the children under `parts`.

Verify with:

```bash
PARENT=<parent_id>
docker exec dp-db psql -U pipeline -d pipeline -c \
  "SELECT page_start,page_end,child_document_id,detected_class,status FROM document_parts WHERE parent_document_id='$PARENT' ORDER BY page_start;"
```

A packet with unclear page boundaries must not be silently converted into one invoice; it remains one document and requires review.

---

## 3. Code test cases (no browser — regression suite)

```bash
cd ~/doc-pipeline/tests

# C1: full E2E incl. delivered-payload assertions (PDF + token checks)
python3 test_api_e2e.py fixtures/PO-2026-8412.pdf "PO-2026-8412,PowerStore,Acceptance checklist,Rack mounting,2026-11-15"
python3 test_api_e2e.py fixtures/IT-Asset-Register-PO-8412.xlsx   # xlsx variant: assert sheets in payload

# C2: batch + dedupe + single-file regression (S6-S8; makes fresh-byte copies itself)
python3 test_batch_e2e.py
```
Both print `PASS/FAIL` per assertion and exit 0 only when everything passes.
✅ 2026-09-18: C1 ALL PASS · C2 ALL PASS (`VALIDATED:2 FLAGGED:1 SKIPPED_DUPLICATE:1`, re-run = 3 skipped, single-file OK).

Manual one-liners:
```bash
EX=http://100.68.212.36:5000
curl -F file=@fixtures/PO-2026-8412.pdf $EX/documents                 # single
curl $EX/documents/<id>                                               # track (raw JSON)
curl "$EX/mock/receiving/inbox?limit=1"                               # exact delivered payload
curl $EX/documents/batch -H 'Content-Type: application/json' -d '{"doc_type":"report","skip_existing":true,"files":[{"name":"a.pdf","data_b64":"'$(base64 -w0 fixtures/PO-2026-8412.pdf)'"}]}'
```

---

## 4. Delivered-output contract (what the receiving API gets)

```json
{ "schema_version": "1.0", "task_id": "...", "document_id": "...",
  "filename": "PO-2026-8412.pdf", "sha256": "...", "uploaded_at": "...",
  "extracted": { "kind": "pdf|xlsx|image", "doc_type": "report", "notes": "...",
                 "pages": [{"page":1,"text":"..."}]      // pdf
                 "sheets":[{"name":"Assets","columns":[...],"rows_preview":[[...]]}]  // xlsx
                 "ocr": "..."                              // reviewed/scanned docs
               } }
```
Validated docs → published to RabbitMQ `job.deliver` → worker POSTs this to `TARGET_API_URL` (today `/mock/receiving`, later your ERP) → `{"accepted":true}` → task DELIVERED. Failures: backoff `5·2ⁿ` s, ≤3 attempts, then `FAILED_PERMANENT`. Content-type is sniffed from magic bytes, so mislabelled uploads still route correctly.

## 5. Auto-OCR (Option 1: Tesseract — live now, no API key)

Flagged scans/photos can heal themselves without Swagger:
`POST /documents/{id}/ocr?lang=eng|ind` — OCRs the stored original (pdftoppm 200dpi → tesseract per page, max 30 pages), merges text into standard JSON, re-validates, and delivers on pass.
Via n8n: `POST http://<host>:5678/webhook/ocr-scan` body `{"document_id":"<id>","lang":"eng","engine":"tesseract|qwen"}` (workflow `docpipeline-ocr-01`; result includes `pages_ocr`, `chars`, `view` link).
**Engine `qwen`** (temporary model `qwen3.8-flash` via Token Plan vision; swap = env `QWEN_MODEL` on dp-extractor) — key lives in extractor env `QWEN_API_KEY` (from `.env`, gitignored), NOT in n8n nodes. Photos/handwriting: Tesseract fails → use qwen.

### U11 · Real MFP scan → OCR → DELIVERED — PASS ✅ (2026-09-18)
19-page Lexmark MX722ade scan (vendor PT SARANA ABADI MAKMUR BERSAMA, SO→FAKTUR→receiving slip → FOODMAX BOGOR). Upload → FLAGGED (0-char text layer) → n8n webhook → **47 s: 19 pages, 28,943 chars, VALIDATED → DELIVERED**, `ocr_engine:tesseract` recorded in payload. Product/brand/customer tokens all present; note known Tesseract digit noise (`0↔6`, `SO-26110209135` → `SOR261 10209135`) — strict-number use cases are why Qwen-VL fallback (Option 2) comes next.

### U12 · Same 19-pager through Qwen (engine=qwen) — PASS ✅ (2026-09-18)
`{"document_id":"…","engine":"qwen"}` → n8n webhook → 19 pages, 29,835 chars, VALIDATED→DELIVERED in **425 s** (~22 s/page, sequential). Quality vs Tesseract: phone/fax digits clean (`(021) 4601849, 4600093`), dates exact (`22-Jul-2026`, `28-Jul-2026`), SO/CPO numbers (`2100115362`), salesman `AHMAD TAHJUDIN`, warehouse/zone codes. Company header partially garbled on the decorative logo area (`PT . . . ANA ABADI` — stamp/logo region), and doc has no literal `SO-` prefix (it's `Sales Order [SO] #`), so token assertions must match real content, not guesses. Re-OCR replaces previous engine text (bug fixed `7992d61`).

**Auto-OCR:** batch uploads auto-trigger OCR for every `FLAGGED` file when `OCR_AUTO=1`; this is enforced by the extractor and does not depend on an n8n payload field. Disable globally only with `OCR_AUTO=0`. `OCR_MAX_PAGES=0` means all pages are processed; set a positive value only when an operational cap is required.
## 6. Dedupe toggle (for testing)

Dedupe sha256 is ON by default (batch → `SKIPPED_DUPLICATE`; single uploads re-ingest but can be blocked by UI flows). To test with the SAME file repeatedly: set `INGEST_ALLOW_DUP=1` in `.env`, then `docker compose up -d extractor` — bypasses ALL dedupe (form/batch/single). Verified: same PDF 3× via batch + 1× via single = 4 separate documents. Return to `0` for production.

## 7. Known gaps (be honest in UAT)

1. ~~OCR human-in-the-loop~~ → Tesseract auto-OCR live (U11); Qwen engine live (U12, model=qwen3.8-flash via QWEN_MODEL env) — dedicated qwen-vl swap pending.
2. **PDF tables arrive as line text**, not cell-structured rows (xlsx does have real rows/cols). (Planned: pdfplumber pass.) — Note: for the RPA batch path this is solved downstream by the deterministic parsers in `batch_map.py` (M-series), not by the extractor.
3. Receiving **inbox view is in-memory** (last 20, cleared on restart); the DB trail in `/view` is permanent.
4. Batch form = one doc_type/notes per submission (per-file metadata needs the API).
5. Typed domain tables are not yet separate; child documents currently map to the shared `invoice_rows` ledger and are distinguished by `doc_class`.
6. `TARGET_API_URL` is still the mock until a real endpoint is set in compose.
7. **Numeric verification is OFF by user decision** (`OCR_NUMERIC_VERIFY=0`, since 2026-09-24): values that fail arithmetic stay `REVIEW-ARITH` with verbatim OCR numbers; the self-learning KB (§9 M5) converts proven rows to `PASS-LEARNED` incrementally, and OCR digit-confusion fixes apply only with exact re-fit proof. Remaining `REVIEW-ARITH` (~2.8K rows in Faktur Penjualan) is the honest open item — candidate for a targeted visual re-OCR pass.
8. 228 PO rows have empty vendor (no vendor text on their source page; ~85 self-recover via `learned_po_vendor` when other booklet pages arrive in future runs).

## 8. FAQ while testing

- Form won't open → Tailscale off. · Workflow edit ignored → must **Publish** (n8n top-right) — production runs the published version. · Payload empty in webhook → send FLAT JSON, never wrap in `{"body":...}`. · Task stuck QUEUED → worker down (`docker logs dp-worker`); sweeper self-heals within ~20 s if the bus message was missed. · n8n login loop → `N8N_SECURE_COOKIE=false` already set (USAGE §3.5).

Operations (start/stop/backup/env) live in **USAGE.md**.

---

## 9. Batch mapping cases (M-series) — Google Sheets RPA tables, live 2026-09-24 → 2026-09-30

Target: fresh **copy** of the `Result RPA` template (sheet id in `copy_sid.txt`; original template never touched). All proofs below run against the **live sheet via API read-back**, never against local counters.

### M1 · Write integrity — PASS ✅
`scripts/read_sheet.py` → `/tmp/sheet_now.json`, compared row-by-row to the pre-write dump (`--dump`).
Final state (2026-09-30): Faktur Penjualan 6.218 · PO Customer 700 · Tanda Terima 842 · Surat Jalan 112 · Faktur Pajak 5.091 · OCR Mapping Review 5.542 · Dokumen Pelunasan 0 (header only, correct). All tabs dump==sheet.

### M2 · e-Faktur full extraction — PASS ✅
1.257/1.257 docs parsed, 1.257 unique faktur numbers in sheet, Σitem = footer Harga Jual on every faktur (0 mismatch), incl. the 3 layout variants (normal / PPN-dibebaskan / multi-item continuation). Cross-check consumed zero LLM tokens (regex over native PDF text layer).

### M3 · Provenance — PASS ✅
All 12.157 `p<page> <filename>` references across all tabs resolve to a real document in the corpus with page number ≤ that document's real page count. Orphan refs: 0.

### M4 · Vendor hygiene (self-learning rules) — PASS ✅
`vendor_rules.json` enforced on read-back: rows with self-company (SARANA ABADI MAKMUR) as vendor: **0** (was 244 before rules). Junk-label vendors (`PT ORDER DATE` etc.): **0**. Placeholder/empty data rows in every tab: **0** (tidy filter permanent in `batch_map.py`). `learned_po_vendor` memory: growing every run (31+ POs memorized at first write).

### M5 · Self-learning variant KB (llm-wiki pattern) — PASS ✅
`scripts/knowledge.py learn/apply/wiki`, wired into `batch_map.py` (apply before write, learn after successful write).
- Promoted semantics: `qty=carton*isi+pcs` for the SAMB sales-booklet variant (1.988 arithmetic-proven evidence rows; threshold ≥3 consistent & ≥60%).
- Effect on sheet: `PASS-LEARNED` 375 rows (relabel only on exact arithmetic proof), `PASS-LEARNED-CORR` 10 rows (single OCR digit fixed via conf rule seen ≥2× in same variant, corrected value re-fits `jumlah` exactly — e.g. `7.600,00→7.800,00`).
- Wiki renders `knowledge/companies/*.md` + `INDEX.md` after every run; contradictions recorded, never silently resolved.
- Independent re-proof of all PASS-LEARNED rows from the sheet alone: 375/375 verify, 0 bad.

### M6 · Date-field repair (Tanda Terima) — PASS ✅
67 rows had column-header text bleeding into Posting Date → repaired from raw page text via provenance line; 314 valid-but-varied formats (`15-SEP-26`, `16/09/26 09:36:53`) normalized to `dd/mm/yyyy`. Remaining blanks: 31 (no date printed on those source pages — honest blank, not fabricated).

### M7 · Coverage completeness — PASS ✅
Every document in both upload windows appears in the sheet (data tabs or review tab): 1.276/1.276, `skipped-OCR-incomplete: []`. Note: e-Faktur rows carry Source File (native PDF, no page refs) — checker must match on `r[18][:28]`, and review rows live in `dump['review']`, not `dump['sheets']`.

Run the whole M-series in one command after any write:
```bash
cd ~/doc-pipeline && python3 scripts/read_sheet.py && python3 scripts/verify_sheet.py
```
