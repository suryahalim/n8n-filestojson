# Doc-Pipeline OCR — Test Playbook (2026-09-19)

Focus: auto-OCR (Tesseract + Qwen via n8n) + dedupe OFF for testing.
Current state: `INGEST_ALLOW_DUP=1` → **same file can be uploaded repeatedly, every copy becomes a new document.** OCR model live = `qwen3.8-flash` (Token Plan). Qwen key sits only inside dp-extractor (`QWEN_API_KEY`) — n8n never touches it.

## 0. Entry points (phone browser OK — Tailscale must be on)

| What | URL |
| --- | --- |
| Upload form (multi-file) | http://100.68.212.36:5678/form/it-upload |
| Viewer (all docs + status) | http://100.68.212.36:5000/view |
| Receiving inbox (delivered JSON) | http://100.68.212.36:5000/view/inbox |
| n8n UI / workflows | http://100.68.212.36:5678 |
| Swagger API | http://100.68.212.36:5000/docs |
| **OCR settings (key/model/endpoint)** | http://100.68.212.36:5000/view/settings |

OCR trigger — since v1.4 it is **automatic**: any FLAGGED doc from a form/batch upload gets OCR'd right after intake (default engine, sequential queue). Manual trigger only needed for re-runs/engine overrides — from n8n (Execute workflow) or curl:
```
POST http://100.68.212.36:5678/webhook/ocr-scan
{"document_id":"<id>","engine":"qwen"}
```
(`"engine":"tesseract"` → Tesseract; omit → **Qwen (default since 2026-09-20)**. `"lang":"eng|ind"` only applies to Tesseract.)

Fixtures (in `~/doc-pipeline/tests/fixtures/`, already sent to this chat):
`PO-2026-8412.pdf` (text PDF) · `SCAN-delivery-note-DN-991.pdf` (flatbed scan, no text layer) · `SCAN-photo-DN-991.jpg` (handheld photo) · `IT-Asset-Register-PO-8412.xlsx`

---

## 1. T1 — Happy path, PDF with text layer (1 min)
1. Form → select `PO-2026-8412.pdf`, doc type `purchase_order`, submit.
2. Result page says VALIDATED + document id.
3. `/view` → row turns green **DELIVERED** within ~5 s. `/view/inbox` → payload contains `PO-2026-8412`, `1,347,873,000`.
✅ Expect: no OCR needed, straight to DELIVERED.

## 2. T2 — Flatbed scan → auto-OCR Tesseract via n8n (2 min)
1. Form → upload `SCAN-delivery-note-DN-991.pdf`. Expect **FLAGGED** (red) — correct: 0-char text layer.
2. Open the doc in `/view`, copy its ID from the URL (`/view/<id>`).
3. n8n → workflow **"DocPipeline - OCR Scan (Tesseract | Qwen)"** → *Execute workflow* → in the first node (On OCR Request) set body:
   `{"document_id":"<id>","engine":"tesseract"}` → run.
4. Response JSON: `engine:"tesseract", pages_ocr:1, chars:~264, validated:true`.
5. `/view` → now **DELIVERED**; inbox payload has `extracted.ocr_engine:"tesseract"`.
✅ Known noise: Tesseract confuses 0↔6 in digits (e.g. `SO-…135` → `SOR261…`). That's why T3 exists.

## 3. T3 — Handheld photo → Qwen OCR (the big one) (3 min)
1. Form → upload `SCAN-photo-DN-991.jpg` (or any phone photo of a document). Expect **FLAGGED**.
2. Same as before, but body: `{"document_id":"<id>","engine":"qwen"}`.
3. Wait ~15–25 s. Response: `engine:"qwen", chars:~1200, validated:true`.
4. `/view/<id>` → OCR text readable end-to-end: D/O No `FD1-26/06/43584`, NPWP, product rows, handwritten date/plate.
✅ Expect: numbers EXACT (compare with T2 noise). Handwriting partially picked up.
⛔ Do NOT run the green carbon DO photo through `engine:"tesseract"` — it returns 0 chars by design (low contrast); pipeline stays FLAGGED = correct behavior.

## 4. T4 — Re-OCR / engine upgrade on the same doc (1 min)
On a doc already DELIVERED via tesseract (from T2), run T3 step 3 again with `engine:"qwen"`.
✅ Expect: text REPLACED (not appended), `ocr_engine` flips to `qwen3.8-flash`, new DELIVERED record in inbox (review_loop increments). Never both engines' text mixed.

## 5. T5 — Duplicates allowed (dedupe OFF) (1 min)
1. Form → upload the SAME file 3× in one multi-select (or batch API 3×).
2. ✅ Expect: **3 separate documents**, 3 distinct IDs — no `SKIPPED_DUPLICATE` anymore.
   (Technical note: single `/documents` upload NEVER deduped — dupes always re-ingested. Dedupe only guarded the BATCH path via sha256; with `INGEST_ALLOW_DUP=1` even that guard is bypassed, so form/batch/API all accept dupes now.)
3. `/view` shows all three rows independently; each can be OCR'd separately.
🔁 This is the testing mode. Production restores §7.

## 6. T6 — Batch mixed upload (2 min)
Form, multi-select ALL four fixtures at once.
✅ Expect response "Batch result — 4 file(s) PROCESSED" (never SKIP in this mode): PDF/XLSX → DELIVERED, scan+photo → FLAGGED (need T2/T3). Excel lands in inbox with 3 structured sheets (Assets/Warranty/MaintenanceLog rows).

## 7. T7 — Manual review-fix (no OCR) (2 min)
For a FLAGGED doc: Swagger → `POST /documents/{id}/correct` with
```json
{"extracted":{"kind":"image","ocr":"<any typed text>","bytes":123}}
```
✅ Expect: doc revalidates → DELIVERED. This is the human path; auto-OCR (T2/T3) is just this same flow automated by n8n.

## 8. T8 — Regression via terminal (optional, on the server)
```bash
python3 ~/doc-pipeline/tests/test_api_e2e.py    # single-upload E2E, token assertions
python3 ~/doc-pipeline/tests/test_batch_e2e.py  # batch — NOTE: expects dedupe ON (U7/U8 era)
```
While `INGEST_ALLOW_DUP=1`, the duplicate assertions inside test_batch_e2e (S7) may report FAIL — that's the toggle, not a bug. Restore §7 before running it.

---


## 9. Mapping invoice → tabel standar + RPA (n8n context, v1.5)

Konteks alur: semua tes ini dimulai dari **form n8n** (`http://100.68.212.36:5678/form/it-upload`) yang sama seperti T1–T8 — n8n hanya menerima file + `doc_type`, sisanya otomatis di extractor. Bedanya kali ini: dokumen **invoice** tidak berhenti di DELIVERED, tapi lanjut masuk tabel standar `invoice_rows`.

### M1 — Happy path invoice foto (3–4 mnt) ⭐ inti fitur
1. Reload form (tab lama = submit senyap gagal), pilih **Document type = Invoice**, upload foto invoice vendor (contoh: DO WhatsApp kemarin).
2. Pantau: `/view` → `FLAGGED` (⏳ ±20 dtk OCR) → teks terisi → **±1–2 mnt kemudian muncul blok hijau "Mapped invoice row"** + tabel Line items & Handwritten.
3. Cek ledger: `http://100.68.212.36:5000/invoices?status=extracted` → barisnya muncul dengan vendor/invoice_number/total terisi.
4. Pass: no. invoice = angka persis tercetak; field tak terbaca = **null + tertulis di `missing[]`** (bukan tebakan); tulisan tangan ada di `handwritten[]` + interpretasi.

### M2 — Invoice multi-halaman (faktur 33 hal punya kamu) (5–12 mnt)
Upload PDF scan >3 halaman sebagai Invoice → hasil mapping pakai strategi digest (hal 1-2 + terakhir penuh, tengah diringkas). Pass: selesai <200 dtk utk mapping, `notes` menjelaskan取舍 (mis. total=0,00 dari halaman 1 karena booklet multi-SO). Ini tes JUJUR-MODEL: dokumen campur-aduk harus menghasilkan confidence low/medium + notes, bukan angka karangan.

### M3 — Siklus RPA penuh (2 mnt, terminal/HTTP, tanpa n8n)
```bash
B=http://100.68.212.36:5000
curl -s $B/invoices?status=extracted | python3 -m json.tool | head -20    # pull antrean RPA
DOC=<document_id dari langkah 1>
curl -s -X PATCH $B/invoices/$DOC -H 'Content-Type: application/json' \
  -d '{"status":"pending_review"}'                                        # RPA klaim
curl -s -X PATCH $B/invoices/$DOC -H 'Content-Type: application/json' \
  -d '{"status":"mapped","rpa_total":1234500,"rpa_note":"koreksi","rpa_status":"corrected"}'   # write-back
curl -s "$B/invoices?status=mapped" | grep -c $DOC                        # lolos ke pool mapped
curl -s $B/invoices/export.csv | head -3                                  # ledger CSV 24 kolom
```
Pass: kolom `total` asli tetap, `rpa_total` terisi; `status` pindah extracted→pending_review→mapped; CSV bisa dibuka Excel.

### M4 — Non-invoice tidak tergoda mapping
Upload foto/kwitansi dengan Document type = **Other** → DELIVERED tanpa blok Mapped, dan TIDAK menambah baris `invoice_rows`. Pass: `/invoices?status=extracted` count tidak naik. (Kalau ternyata dokumen "Other" ternyata invoice, selalu bisa manual: `POST /documents/{id}/map {"doc_type":"invoice"}` → ledger terisi.)

### M5 — Biaya token (jawaban cepat saat audit)
- `GET/PATCH /invoices*` & export CSV = **nol token** (pure Postgres).
- 1 invoice foto ≈ 1 OCR call (±2.5K tok) + 1 map call (±4-6K in, ±1-2K out). 1 faktur 33 hal ≈ 30 OCR + 1 map.
- Cek model aktif: `/view/settings`. Kalau ragu, mapping gagal = DELIVERED tetap jalan (mapping failure tidak memblokir delivery).

## 10. Where to watch while testing
- `/view` — status per document (green DELIVERED / red FLAGGED / amber).
- `/view/inbox` — exact JSON received by the downstream API (schema `document_id, filename, sha256, extracted{…}`).
- n8n → Executions — every OCR call is a visible run (timing, errors).
- Errors? `docker logs dp-extractor --since 10m | tail -20` (on the server).

## 11. Tuning knobs (env on dp-extractor, set in ~/doc-pipeline/.env + `docker compose up -d extractor`)
| Var | Current | Meaning |
| --- | --- | --- |
| `QWEN_MODEL` | qwen3.8-flash | swap to `qwen-vl-ocr` etc. — no code change |
| `QWEN_API_KEY` | set | Token Plan key (never in n8n/repo) |
| `OCR_ENGINE` | **qwen (default)** | fallback engine when caller omits `engine` — change to `tesseract` anytime |
| `OCR_AUTO` | **1 (on)** | 1 = flagged docs auto-OCR immediately after form/batch upload; 0 = manual n8n trigger only |
| `INGEST_ALLOW_DUP` | **1 (testing)** | 0 = dedupe sha256 ON for production |
| `OCR_MAX_PAGES` | 30 | pages OCR'd per doc |

## 12. After testing — restore production behavior
```bash
cd ~/doc-pipeline && sed -i 's/^INGEST_ALLOW_DUP=1/INGEST_ALLOW_DUP=0/' .env && docker compose up -d extractor
```
Then a duplicate upload must again show `SKIPPED_DUPLICATE` (batch) — run `test_batch_e2e.py` to prove it.

## Pass criteria summary (UAT checklist)
- [ ] T1 PDF → DELIVERED ≤5 s, no OCR
- [ ] T2 scan → FLAGGED → tesseract → DELIVERED (~5 s/page), digit noise acknowledged
- [ ] T3 photo → FLAGGED → qwen → DELIVERED (~20 s), numbers exact
- [ ] T4 re-OCR replaces old engine text cleanly
- [ ] T5 same file ×3 → 3 documents (dedupe off)
- [ ] T6 batch 4 files → 2 DELIVERED + 2 FLAGGED, xlsx structured
- [ ] T7 manual correct → DELIVERED
- [ ] inbox JSON matches contract every time (document_id + sha256 + extracted.kind)
- [ ] M1 photo invoice → Mapped row + ledger row, null-not-guess rule holds
- [ ] M2 33-page factuur maps <200 s with honest notes/confidence
- [ ] M3 RPA cycle extracted→pending_review→mapped + CSV export
- [ ] M4 doc_type=Other never enters invoice_rows
