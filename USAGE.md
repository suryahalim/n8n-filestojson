# Doc Pipeline — User Guide & Testing

Document ingestion → validation → OCR → service-bus delivery — extractor-native since 2026-10-02 (n8n removed; old workflows archived in `archive/n8n-2026-10-02/`).
**Testing: `TESTING.md` — U-series (UI cases U0–U10) + C-series (regression) + output contract.**
Implements the flow: **Upload → Save original + job → Extract → Standard JSON → Validation → (pass) Push to receiving API via Service Bus → (fail) Auto-OCR or review loop → retry/complete.**

---

## 1. Components & URLs

| Component | URL (from your laptop/phone on Tailscale) | Notes |
|---|---|---|
| Extractor API | http://100.68.212.36:5000 | upload & status API, viewer, settings |
| Folder upload UI | http://100.68.212.36:5000/view/upload | **whole folder (DFS)**, live progress |
| Documents view | http://100.68.212.36:5000/view | status per doc + folder provenance |
| RabbitMQ mgmt | http://100.68.212.36:15672 | user `pipeline` / pw `.env` (`RABBITMQ_PASS`) — watch queues |
| Postgres | internal `dp-db` only | data lives in volume `dp-pgdata` |

Server-only local: `127.0.0.1:5000` via SSH.

## 1.5 Upload — Form UI (no terminal needed)

**URL:** http://100.68.212.36:5000/view/upload (Tailscale on; no login)

1. **Pick a folder** (subfolders included — DFS walks the whole tree) or individual files → optional **doc type hint** → **Upload & start batch OCR**.
2. Server answers immediately with the file count; the page shows a live progress bar (`GET /documents/folder/status`) and per-file results as they land.
3. Behind it: each file is spooled to disk, then ingested + auto-OCR'd by 4 parallel workers (env `OCR_WORKERS`). Valid text PDF/XLSX → `VALIDATED → DELIVERED` in seconds. Scans/images → `FLAGGED` → OCR right away → `VALIDATED`. Duplicate bytes → `SKIPPED_DUPLICATE` (sha256).
4. Folder + relative path of every document are stored (`documents.folder` / `rel_path`) — audit per original bundle/customer tree.
5. Big trees without the browser: drop the folder under `~/doc-pipeline/data/` and call `POST /documents/folder/scan {"path":"/data/inbox/<dir>"}`.

Single small files from a phone: the API `POST /documents` (Swagger `/docs`) or the folder UI with a one-file folder.

## 2. Quick test from a terminal (the fastest "does it work")

```bash
EX=http://100.68.212.36:5000

# A. Upload a real PDF (or xlsx). Expect: "status":"VALIDATED" + a delivery_task id
curl -F file=@/path/to/invoice.pdf $EX/documents

# B. Upload a blank/scanned PDF or image without OCR -> expect "status":"FLAGGED"
#    (auto-OCR chains it immediately when OCR_AUTO=1; disable with OCR_AUTO=0)
curl -F file=@scan-of-receipt.jpg $EX/documents

# C. Follow the document's journey
curl $EX/documents/<document_id>       # shows standard_json, validation, jobs, delivery_tasks
curl $EX/documents/<document_id>/track # event trail

# D. Review loop: submit corrected extraction (e.g. OCR text) -> revalidates -> delivers
curl -X POST $EX/documents/<document_id>/correct \
  -H 'Content-Type: application/json' \
  -d '{"extracted":{"kind":"pdf","page_count":1,"pages":[{"page":1,"text":"Full corrected text here ..."}]}}'

# E. Manual OCR trigger (same call the auto-chain makes)
curl -X POST "$EX/documents/<document_id>/ocr?engine=qwen"
```

Expected status transitions per document:
`UPLOADED → VALIDATED → DELIVERED` (happy path) or
`UPLOADED → FLAGGED → (auto-OCR / review) → VALIDATED → DELIVERED` or `DELIVERY_FAILED` after 3 attempts.

## 3. Test the retry mechanism (failure path)

```bash
EX=http://100.68.212.36:5000
curl -X POST $EX/mock/receiving/fail-next      # next delivery gets a simulated 503
curl -F file=@invoice.pdf $EX/documents        # note the delivery_task id
# ~1-2s later: task = RETRY_WAIT, attempts=1  (curl $EX/documents/<id> to confirm)
# ~5s later:   worker sweeper republishes it -> task = DELIVERED
```

After 3 failed attempts a task becomes `FAILED_PERMANENT` and the document `DELIVERY_FAILED` (visible in `/documents/<id>` and Postgres).

## 4. Review loop (human correction)

A `FLAGGED` document that OCR cannot fix (or needs transcription) is corrected through `POST /documents/{id}/correct` (§2D): send the corrected `extracted` fields, the extractor re-validates, and on pass publishes delivery like every other path. Viewer `/view/<id>` shows the raw text, validation flags, and the delivered payload.

## 5. Watch the Service Bus (RabbitMQ)

- Open http://100.68.212.36:15672 → Admin → Queues: `job.deliver` shows *Ready / Unacked / consumers=1 (dp-worker)*.
- Messages only queue up if the worker is down — publishing while the worker is healthy drains instantly (that's normal).

## 6. Operate the stack

```bash
cd ~/doc-pipeline
docker compose ps                      # health of all 4 services (db, rabbit, extractor, worker)
docker compose logs -f worker          # delivery consumer
docker compose logs -f extractor | grep -E "FOLDER-INGEST|AUTO-OCR"   # intake/OCR
docker compose restart worker extractor
docker compose up -d                   # apply changes / after reboot
```

Data locations: uploads `~/doc-pipeline/data/uploads/` (deduped by sha256 prefix), folder spool `~/doc-pipeline/data/spool/` (auto-cleaned), inbox for server-scan `~/doc-pipeline/data/inbox/`, DB volume `dp-pgdata`, queue volume `dp-rabbit`. Secrets: `~/doc-pipeline/.env` (chmod 600).

## 7. Where things plug in next

- **Real receiving API**: set `TARGET_API_URL` in `docker-compose.yml` (extractor env) to your actual endpoint instead of `/mock/receiving`.
- **Mapping results (DB is destination):** UI `http://<host>:5000/view/tables` (grid + CSV per tab), monitor `http://<host>:5000/view/mapping`. Full guide: `MAPPING_GUIDE.md`.
- **Google Sheets export**: optional/legacy agent-side path (`scripts/batch_map.py` + `po_v2.py`, manual) — see `RESUME.md`.
- **Other input channels** (email, Google Drive, WhatsApp): POST multipart to `/documents` or `/documents/folder` — the pipeline stays identical.
- **Later ERP RPA**: read/claim the Postgres ledger via `/invoices` endpoints (zero LLM tokens).

## 8. Troubleshooting

| Symptom | Check |
|---|---|
| Upload returns 500 "extract failed" | `docker compose logs extractor` — usually unsupported MIME (file stored, job marked FAILED) |
| Status stuck at VALIDATED, never DELIVERED | worker down? `docker compose logs worker`; queue backing up in :15672 |
| Folder upload returns 409 | another folder job is running — `GET /documents/folder/status` |
| Folder upload "spool full" | disk low: `df -h`, clear `data/spool/` leftovers |
| Forgot where a doc is | `curl $EX/documents?limit=200` then `curl $EX/documents/<id>` for the full trail |
