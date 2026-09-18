# Doc Pipeline — User Guide & Testing

Document ingestion → validation → service-bus delivery, orchestrated with n8n.
**Testing: `TESTING.md` — U-series (UI cases U0–U10) + C-series (regression) + output contract. All green as of 2026-09-18.**
Implements the flow: **Upload → Save original + job → Extract → Standard JSON → Validation → (pass) Push to receiving API via Service Bus → (fail) Review loop → retry/complete.**

---

## 1. Components & URLs

| Component | URL (from your laptop/phone on Tailscale) | Notes |
|---|---|---|
| n8n editor | http://<host>:5678 | login: `N8N_OWNER_EMAIL` / `N8N_OWNER_PASS` from `.env` |
| Extractor API | http://100.68.212.36:5000 | the upload & status API |
| RabbitMQ mgmt | http://100.68.212.36:15672 | user `pipeline` / pw `.env` (`RABBITMQ_PASS`) — watch queues |
| Postgres | internal `dp-db` only | data lives in volume `dp-pgdata` |

Server-only local: `127.0.0.1:5678`, `127.0.0.1:5000` via SSH.

## 1.5 Upload from your phone — Form UI (no terminal needed)

**URL:** http://100.68.212.36:5678/form/it-upload (Tailscale on; no login)

1. Pick a file (PDF/Excel/image, ≤25 MB) → choose **Document type** → optional **Notes** → **Upload**
2. Result screen shows status + Document ID; track it at `http://100.68.212.36:5000/documents/<id>`
3. Valid text PDF/XLSX deliver automatically in seconds (`VALIDATED → DELIVERED`). Scans/images land `FLAGGED` → OCR/review path (§4).
4. Behind it: n8n workflow `docpipeline-form-upload` — Form trigger → Normalize Fields (Code) → Send to Extractor → Respond. `doc_type`/`notes` travel as **query params** (n8n's HTTP node silently drops non-file multipart fields — normalized to safe tokens like `purchase_order`).
5. Audit every submission: n8n → Executions tab.

## 2. Quick test from a terminal (the fastest "does it work")

```bash
EX=http://100.68.212.36:5000

# A. Upload a real PDF (or xlsx). Expect: "status":"VALIDATED" + a delivery_task id
curl -F file=@/path/to/invoice.pdf $EX/documents

# B. Upload a blank/scanned PDF or image without OCR -> expect "status":"FLAGGED"
#    and a list of flagged_fields (this triggers the n8n review webhook)
curl -F file=@scan-of-receipt.jpg $EX/documents

# C. Follow the document's journey
curl $EX/documents/<document_id>       # shows standard_json, validation, jobs, delivery_tasks

# D. Review loop: submit corrected extraction (e.g. OCR text) -> revalidates -> delivers
curl -X POST $EX/documents/<document_id>/correct \
  -H 'Content-Type: application/json' \
  -d '{"extracted":{"kind":"pdf","page_count":1,"pages":[{"page":1,"text":"Full corrected text here ..."}]}}'
```

Expected status transitions per document:
`UPLOADED → VALIDATED → DELIVERED` (happy path) or
`UPLOADED → FLAGGED → (review) → VALIDATED → DELIVERED` or `DELIVERY_FAILED` after 3 attempts.

## 3. Test the retry mechanism (failure path)

```bash
EX=http://100.68.212.36:5000
curl -X POST $EX/mock/receiving/fail-next      # next delivery gets a simulated 503
curl -F file=@invoice.pdf $EX/documents        # note the delivery_task id
# ~1-2s later: task = RETRY_WAIT, attempts=1  (curl $EX/documents/<id> to confirm)
# ~5s later:   worker sweeper republishes it -> task = DELIVERED
```

After 3 failed attempts a task becomes `FAILED_PERMANENT` and the document `DELIVERY_FAILED` (visible in `/documents/<id>` and Postgres).

## 3.5 Security note on `N8N_SECURE_COOKIE=false`

n8n marks its login cookie `Secure` by default, which browsers reject over plain HTTP → login loops / cookie never set. We set `N8N_SECURE_COOKIE=false` because access is `http://100.68.212.36:5678` (Tailscale-only, already encrypted at the network layer; no LAN/public exposure). Accepted risk for a personal lab; if a real domain + TLS (Caddy/Traefik + Let's Encrypt) is added, remove the flag.

## 4. Test via the n8n UI

1. Open http://100.68.212.36:5678 → login → **Workflows** → **"Doc Pipeline - Review Flagged Fields"**.
2. The workflow has two entries:
   - `Webhook: Flagged Docs` (`/webhook/review-flagged`) — called automatically by the extractor when validation fails; it fetches the document, switches by kind, and responds "review_required".
   - `Webhook: Review Complete` (`/webhook/review-complete`) — you (or a human-review UI) call this with corrected fields; it posts them to `/documents/{id}/correct` and reports `validated: true/false`.
3. Manual end-to-end inside n8n — the workflow is **Active**, so use the PRODUCTION URL directly:
   ```bash
   # send a corrected extraction for a flagged document (payload is FLAT, not wrapped):
   curl -X POST http://100.68.212.36:5678/webhook/review-complete \
     -H 'Content-Type: application/json' \
     -d '{"document_id":"<id>","extracted":{"kind":"image","ocr":"recovered text ...","bytes":342}}'
   # -> {"routed":true,"validated":true,"delivery_task":"..."} ; document becomes DELIVERED
   ```
   (Inside the flow, n8n wraps this as `$json.body`, which the nodes read — don't send `{"body":{...}}` yourself.)
4. **Executions** tab = audit log of every review-loop hit (timestamped, with payloads).

## 5. Watch the Service Bus (RabbitMQ)

- Open http://100.68.212.36:15672 → Admin → Queues: `job.deliver` shows *Ready / Unacked / consumers=1 (dp-worker)*.
- Messages only queue up if the worker is down — publishing while the worker is healthy drains instantly (that's normal).

## 6. Operate the stack

```bash
cd ~/doc-pipeline
docker compose ps                      # health of all 5 services
docker compose logs -f worker          # delivery consumer
docker compose logs -f extractor       # upload/extract API
docker compose restart worker extractor
docker compose up -d                   # apply changes / after reboot
```

Data locations: uploads `~/doc-pipeline/data/uploads/` (deduped by sha256 prefix), DB volume `dp-pgdata`, n8n volume `dp-n8n`, queue volume `dp-rabbit`. Secrets: `~/doc-pipeline/.env` (chmod 600).

## 7. Where things plug in next

- **Real receiving API**: set `TARGET_API_URL` in `docker-compose.yml` (extractor env) to your actual endpoint instead of `/mock/receiving`.
- **Real OCR for images**: the extractor flags images with `extracted.ocr == null`; wire the n8n review workflow to call an OCR node/HTTP service, then post the result to `/documents/{id}/correct` — the loop is already in place.
- **Other input channels** (email, Google Drive, WhatsApp): add n8n trigger nodes that `POST multipart` to `/documents` — the pipeline stays identical.

## 8. Troubleshooting

| Symptom | Check |
|---|---|
| Upload returns 500 "extract failed" | `docker compose logs extractor` — usually unsupported MIME (file stored, job marked FAILED) |
| Status stuck at VALIDATED, never DELIVERED | worker down? `docker compose logs worker`; queue backing up in :15672 |
| `webhook ... not registered` in n8n | workflow must be **Active** (toggle top-right); test URL = `/webhook-test/...`, production = `/webhook/...` |
| n8n login wrong | owner email/pw come from `~/doc-pipeline/.env`; owner was claimed via `/rest/owner/setup` |
| Forgot where a doc is | `curl $EX/documents | grep <filename>` then `curl $EX/documents/<id>` for the full trail |
