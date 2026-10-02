# RESUME — state & prosedur (2026-10-01)

Dokumen ini ada supaya sesi baru bisa lanjut tanpa mengulang dari nol.
Semua file yang disebut di sini sudah ter-commit dan ter-push ke `origin/main`.

## 1. Titik keadaan (final, terverifikasi live)

| Tab | Baris live | Keterangan |
|---|---|---|
| PO Customer | **940** | 13 kolom (skema rules `po_v2`) — SEMUA baris punya Product Name, 0 pelanggaran aritmetika MAPPED, 0 SAMB-as-issuer, PPN murni `11%`/`1.1%` |
| Faktur Penjualan | 6.218 | skema 16 kolom |
| Faktur Pajak | 5.091 | 1.257 dokumen PASS / 0 REVIEW |
| Tanda Terima | 842 | |
| Surat Jalan | 112 | |
| OCR Mapping Review | 5.542 | halaman mentah yang belum terpetakan penuh |
| Dokumen Pelunasan | header-only | by design: batch ini tidak berisi dokumen pelunasan |

- **Sheet (working copy):** `16tlVDj91ratVSlaZnRmDkUDs9Gtd_D7qV4d4LV1XKUs` (juga di `copy_sid.txt`).
  PO Customer gid `1109169821`. JANGAN menulis ke template asli `Result RPA`.
- **Dump mapping final:** `snapshots/dump_final_2026-10-01.json` (= `/tmp/dump_v2g.json`)
- **Read-back sheet:** `snapshots/sheet_live_2026-10-01.json`
- **Cache korpus OCR (semua halaman semua dokumen):** `snapshots/batch_map_docs_2026-10-01.json.gz` (13 MB terkompres; key = **document id**, bukan nama file)
- **Checkpoint deep-verify:** `knowledge/dv_queue.json` (192 halaman selesai) + `snapshots/dv_queue_2026-10-01.json`; override terbukti di `knowledge/overrides.json`


## 1b. Folder / DFS ingestion (baru 2026-10-02, commit e782fc6)
- UI: `http://100.68.212.36:5000/view/upload` — pilih folder → DFS upload seluruh tree → ingest + auto-OCR paralel (OCR_WORKERS=4), progress live.
- API: `POST /documents/folder` (multipart browser), `POST /documents/folder/scan {"path":"/data/inbox/..."}` (folder di disk NUC, mount `./data` → `/data` di container), `GET /documents/folder/status`.
- **n8n DIHAPUS 2026-10-02** (commit berikutnya): container dp-n8n & port 5678 tidak ada lagi; intake/review/OCR 100% extractor-native (`/documents/{id}/ocr`, `/correct`, auto-chain). Definisi workflow lama: `archive/n8n-2026-10-02/`. Jangan pakai URL :5678 mana pun.
- Provenance tersimpan di kolom `documents.folder` + `rel_path` (migrasi db-init/07 — sudah diterapkan live).

## 2. Rules PO (authoritative)
Terdokumentasi di `knowledge/po_rules.json`; implementasi `scripts/po_v2.py` + `scripts/item_fallback.py`:
1. **PPN** hanya `11%` atau `1.1%`.
2. **Vendor Code** = kode SAMB di mata client (label `VENDOR : 300045730` → `300045730`); BUKAN nama; tidak ada label → kosong.
3. **PO Issuer** = customer SAMB, **selalu diawali PT**; sumber berjenjang: label WP/penerbit → baris PT non-self di header → alias brand→PT terbukti (`knowledge/issuers.json`) → nama toko + tag `|ISSUER-STORENAME`.
4. **Total = Qty × Unit Price − Discount** (tol 2%, gate aritmetika; gagal → `REVIEW-ARITH`, tidak pernah dikarang).
5. **SAMB tidak pernah** jadi vendor/issuer (varian OCR rusak tercakup regex).
6. **Setiap baris PO wajib punya Product Name**; baris tanpa nama dibuang dari tab (halaman mentahnya tetap di tab Review). Nama sampah header (`Halaman`, `TOTAL QTY`, `- Jumlah Kekurangan`) difilter.

## 3. Cara menjalankan ulang (pipeline lengkap)
```bash
cd ~/doc-pipeline
# windows mapping yang dipakai (WAJIB sama dengan ini):
python3 scripts/batch_map.py --dump /tmp/dump_run.json --dry-run \
  --window '2026-09-24T12:00|2026-09-24T12:20|efaktur' \
  --window '2026-09-24T13:00|2026-09-26T23:59|scans'
# koreksi deep-verify (override terbukti) lalu tulis ke sheet + format + read-back:
python3 scripts/dv_finalizer.sh          # apply overrides + rewrite + verify
# ATAU manual:
python3 scripts/_write_v2f.py           # tulis dump_v2g + format PO + verifikasi
```
Extractor/OCR viewer: `http://100.68.212.36:5000` (container `dp-extractor`), status OCR via `GET /documents/<docid>`.

## 4. Auth Google (Drive/Spreadsheets)
- Token: `~/.hermes/google_token.json` (refresh token aktif, scopes drive+spreadsheets).
- **Jika token revoked/`invalid_grant`:** jalankan `python3 scripts/oauth_manual.py` → kirim URL consent ke user → user paste URL `localhost/?code=...` → simpan ke file → `python3 scripts/_oauth_finish3.py` (flow PKCE yang verifier-nya tersimpan; skrip lama `oauth_finish.py` menyimpan verifier juga).

## 5. Known gaps / pekerjaan berikutnya
- 130 `REVIEW-ARITH` + 122 `OCR_ITEM` di PO = kandidat loop vision `deep_verify.py` — **belum diadaptasi ke skema 13 kolom** (indeks lama 12 kolom di file itu).
- ~191 halaman PO masih `PO-HEADER-NOITEMS`/raw-only di tab Review (scan jelek).
- Issuer 3 baris `OCR_PAGE_REVIEW` lama (tanpa nama) sudah hilang dari tab; entri Review tetap sumber audit.
- Viewer `f44358d3` (7000363000) di cache lama 166 halaman; API live 279 → korpus penuh untuk dokumen itu belum di-fetch ulang; halaman 167–279 belum dipetakan.
- Jangan pernah set `MAP_AUTO=1`; mapping hanya atas izin user. `OCR_NUMERIC_VERIFY=0`.

## 6. Konvensi kerja (jangan dilanggar)
- Mapping hanya ke **copy**, backup snapshot sebelum menulis.
- Nilai tidak terbaca → kosong jujur / tag status; tidak pernah menebak angka.
- Schema berubah → lewat migrasi, bukan DDL runtime (aturan user).
- Perubahan KB = self-learn: tulis ke `knowledge/*.json` dengan bukti, bukan hardcode tebakan.
- Commit + push setiap selesai satu workstream (repo ini = backup utama user).
