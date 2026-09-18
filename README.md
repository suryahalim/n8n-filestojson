# Doc Pipeline

IT document ingestion → extraction → validation → service-bus delivery, orchestrated with n8n.

**Flow:** Upload (n8n Form, multi-file, or API) → save original + job (Postgres) → extract (pypdf/openpyxl, content-sniffing) → standard JSON → validation → pass: publish to RabbitMQ (`job.deliver`) → worker pushes JSON to receiving API with retry/backoff → `DELIVERED`. Fail validation: `FLAGGED` → human/OCR review via `/documents/{id}/correct` → revalidate → deliver. Duplicates (sha256) are skipped, never re-delivered.

## Stack

| Service | Image | Port (host) |
|---|---|---|
| n8n (form + workflows) | n8nio/n8n | 5678 |
| Postgres (state) | postgres:16 | internal |
| RabbitMQ (service bus) | rabbitmq:4-management | 15672 |
| Extractor API + views | custom FastAPI | 5000 |
| Delivery worker | custom Python | internal |

**UIs:** upload form `/form/it-upload` · result dashboard `:5000/view` · Swagger `:5000/docs` · n8n · RabbitMQ console.

## Quick start

```bash
cp .env.example .env        # fill real secrets
docker compose up -d --build
# first run: claim n8n owner, then import workflows:
docker cp n8n/workflow-form-upload.json dp-n8n:/tmp/wf.json
docker exec dp-n8n n8n import:workflow --input=/tmp/wf.json
docker exec dp-n8n n8n update:workflow --id=docpipeline-form-upload --active=true
docker exec dp-n8n n8n publish:workflow --id=docpipeline-form-upload   # publish or prod runs stale version!
# same for workflow-review-loop.json (id docpipeline-review-01)
```

Bind ports to specific interfaces in `docker-compose.yml` (this deployment exposes on localhost + a Tailscale IP only). Over plain HTTP set `N8N_SECURE_COOKIE=false` on the n8n service (local/Tailscale only; re-enable with HTTPS).

## Verify

```bash
python3 tests/test_api_e2e.py            # single-file E2E w/ payload assertions
python3 tests/test_batch_e2e.py          # batch + dedupe + regression
```

Full manual test-case suite (UI-only, U0–U10): **TESTING.md**. Operations/troubleshooting: **USAGE.md**.

## Real receiving system

Set `TARGET_API_URL` (worker env) to your endpoint. Contract: POST JSON, respond `{"accepted": true}` — schema in TESTING.md §4. Until then delivery goes to the built-in mock (`:5000/mock/receiving`, receipts at `/view/inbox`).

## Layout

```
docker-compose.yml        # 5 services
db-init/                  # postgres schema + n8n db creation
extractor/                # FastAPI: upload/batch/track/views/correct/mock
worker/                   # RabbitMQ consumer w/ backoff + sweeper
n8n/                      # workflow JSONs: form-upload (multi-file), review-loop
tests/                    # e2e scripts + fixtures (PO story S1–S8)
```

No API keys anywhere in the pipeline: PDF/XLSX parsing is local (pypdf/openpyxl); scanned/image docs require an OCR pass (currently human review; Tesseract/Qwen auto-OCR planned).
