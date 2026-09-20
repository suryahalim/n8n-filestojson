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

**Key design rule:** API keys and model names live ONLY in the extractor (env from `~/doc-pipeline/.env`). n8n never holds or forwards secrets — its OCR workflow only routes `document_id` + engine choice to extractor endpoints. Swapping OCR models = env change + restart, zero workflow edits.

## Configuration (single source: `~/doc-pipeline/.env`, gitignored, mode 600)

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
| `INGEST_ALLOW_DUP` | `1` (testing) | `0` = sha256 dedupe ON for production |
| `OCR_MAX_PAGES` / `OCR_DPI` | `30` / `200` | OCR page cap / render DPI |
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

## Maintenance runbook

**Change OCR model:** edit `QWEN_MODEL` in `.env` → `docker compose up -d extractor` → test: `curl -X POST .../webhook/ocr-scan -d '{"document_id":"<flagged-id>","engine":"qwen"}'` → expect `engine:"qwen"` DELIVERED. Verify model exists in plan first (unavailable models return HTTP 404).

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
- Operations & UI walkthrough: `USAGE.md` · UI test suite (U-series): `TESTING.md` · OCR-focused playbook (T1–T8): `OCR_TEST_PLAYBOOK.md`
- Receiving contract: `TESTING.md` §4 · Real endpoint switch: set `TARGET_API_URL`
- n8n workflows source of truth: `n8n/*.json` (keep committed copies in sync with live DB after every publish)
