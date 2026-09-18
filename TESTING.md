# Doc Pipeline — Test Case Suite & Documentation

Version: 2026-09-18 · All cases executed live on this date — **PASS** (U10 is the composite of U1–U4+U7, each verified live today; run it as one sitting during real UAT).

**Rule of thumb:** UI cases (U-series) = what your staff/stakeholders will do. Code cases (C-series) = regression checks you (or CI) run once before declaring any change safe.

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
Via n8n: `POST http://<host>:5678/webhook/ocr-scan` body `{"document_id":"<id>","lang":"eng"}` (workflow `docpipeline-ocr-01`; result includes `pages_ocr`, `chars`, `view` link).

### U11 · Real MFP scan → OCR → DELIVERED — PASS ✅ (2026-09-18)
19-page Lexmark MX722ade scan (vendor PT SARANA ABADI MAKMUR BERSAMA, SO→FAKTUR→receiving slip → FOODMAX BOGOR). Upload → FLAGGED (0-char text layer) → n8n webhook → **47 s: 19 pages, 28,943 chars, VALIDATED → DELIVERED**, `ocr_engine:tesseract` recorded in payload. Product/brand/customer tokens all present; note known Tesseract digit noise (`0↔6`, `SO-26110209135` → `SOR261 10209135`) — strict-number use cases are why Qwen-VL fallback (Option 2) comes next.

## 6. Known gaps (be honest in UAT)

1. ~~OCR human-in-the-loop~~ → Tesseract auto-OCR live (U11); Qwen-VL fallback for noisy digit/precision docs pending API key.
2. **PDF tables arrive as line text**, not cell-structured rows (xlsx does have real rows/cols). (Planned: pdfplumber pass.)
3. Receiving **inbox view is in-memory** (last 20, cleared on restart); the DB trail in `/view` is permanent.
4. Batch form = one doc_type/notes per submission (per-file metadata needs the API).
5. `TARGET_API_URL` is still the mock until a real endpoint is set in compose.

## 7. FAQ while testing

- Form won't open → Tailscale off. · Workflow edit ignored → must **Publish** (n8n top-right) — production runs the published version. · Payload empty in webhook → send FLAT JSON, never wrap in `{"body":...}`. · Task stuck QUEUED → worker down (`docker logs dp-worker`); sweeper self-heals within ~20 s if the bus message was missed. · n8n login loop → `N8N_SECURE_COOKIE=false` already set (USAGE §3.5).

Operations (start/stop/backup/env) live in **USAGE.md**.
