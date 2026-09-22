"""Extractor service — implements: upload -> save original + create job ->
extract -> standard JSON -> validation.
Validation pass: publish to Service Bus (RabbitMQ exchange 'doc_pipeline',
routing key 'job.deliver') + commit delivery task row (transaction with job DONE).
Validation fail: flag fields + notify n8n review webhook (the loop back).
Also hosts /mock/receiving as stand-in downstream system."""
import os, io, json, base64, glob, hashlib, uuid, datetime, urllib.request
import time
import threading
import subprocess, tempfile
import logging
log = logging.getLogger("extractor")
from pathlib import Path
import psycopg2, psycopg2.extras
from fastapi.responses import FileResponse
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request, Response
import pika

UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "/data/uploads"))
DATABASE_URL = os.environ["DATABASE_URL"]
RABBIT_HOST = os.environ.get("RABBIT_HOST", "rabbitmq")
RABBIT_USER = os.environ.get("RABBIT_USER", "pipeline")
RABBIT_PASS = os.environ.get("RABBIT_PASS", "")
N8N_REVIEW_WEBHOOK = os.environ.get("N8N_REVIEW_WEBHOOK", "http://n8n:5678/webhook/review-flagged")
TARGET_API_URL = os.environ.get("TARGET_API_URL", "http://extractor:5000/mock/receiving")

app = FastAPI(title="doc-pipeline extractor")
psycopg2.extras.register_default_jsonb(loads=json.loads)


def db():
    return psycopg2.connect(DATABASE_URL)


UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def extract_pdf(raw: bytes):
    from pypdf import PdfReader
    r = PdfReader(io.BytesIO(raw))
    pages = []
    for i, p in enumerate(r.pages):
        try:
            pages.append({"page": i + 1, "text": p.extract_text() or ""})
        except Exception as e:
            pages.append({"page": i + 1, "text": "", "error": str(e)})
    return {"kind": "pdf", "page_count": len(r.pages), "pages": pages}


def extract_xlsx(raw: bytes):
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(raw), data_only=True)
    sheets = []
    for ws in wb.worksheets:
        sheets.append({"name": ws.title, "row_count": ws.max_row,
                       "columns": [str(c.value) if c.value else "" for c in ws[1]],
                       "rows_preview": [[str(c.value) if c.value is not None else "" for c in r]
                                        for r in list(ws.iter_rows(max_row=min(ws.max_row, 25)))]})
    return {"kind": "xlsx", "sheets": sheets}


def extract_image(raw: bytes):
    return {"kind": "image", "ocr": None, "bytes": len(raw),
            "note": "image stored for OCR stage (tesseract on worker)"}


EXTRACTORS = {
    "application/pdf": extract_pdf,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": extract_xlsx,
    "image/jpeg": extract_image,
    "image/png": extract_image,
    "image/webp": extract_image,
    "text/plain": lambda raw: {"kind": "text", "text": raw.decode("utf-8", "replace")},
}


def validate(env: dict):
    flags = []
    ext = env["extracted"]
    if ext["kind"] == "pdf":
        if not ext.get("pages"):
            flags.append({"field": "extracted.pages", "rule": "pdf must have >=1 page"})
        text_all = " ".join(p["text"] for p in ext.get("pages", []))
        if len(text_all.strip()) < 20:
            flags.append({"field": "extracted.pages.text", "value": "near-empty",
                          "rule": "min 20 chars (scanned/blank pdf? needs OCR)"})
    if ext["kind"] == "xlsx":
        if not ext.get("sheets"):
            flags.append({"field": "extracted.sheets", "rule": "at least one sheet"})
    if ext["kind"] == "image" and not ext.get("ocr"):
        flags.append({"field": "extracted.ocr", "value": None, "rule": "image requires OCR result"})
    return (len(flags) == 0), flags


def publish(job_type: str, payload: dict):
    creds = pika.PlainCredentials(RABBIT_USER, RABBIT_PASS)
    conn = pika.BlockingConnection(pika.ConnectionParameters(
        host=RABBIT_HOST, credentials=creds, heartbeat=600))
    ch = conn.channel()
    ch.exchange_declare(exchange="doc_pipeline", exchange_type="direct", durable=True)
    ch.queue_declare(queue="job." + job_type, durable=True)
    ch.queue_bind(queue="job." + job_type, exchange="doc_pipeline", routing_key="job." + job_type)
    ch.basic_publish(exchange="doc_pipeline", routing_key="job." + job_type,
                     body=json.dumps(payload),
                     properties=pika.BasicProperties(delivery_mode=2))
    conn.close()


KNOWN_MIMES = set(EXTRACTORS)
MIME_SUFFIX = {"application/pdf": "pdf",
               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
               "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp",
               "text/plain": "txt"}


def sniff_mime(raw: bytes, declared: str, filename: str) -> str:
    """Best-effort content type: declared header -> magic bytes -> file extension.
    Guards against clients (curl, some proxies) sending application/octet-stream."""
    if declared in KNOWN_MIMES:
        return declared
    if raw[:4] == b"%PDF":
        return "application/pdf"
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    if declared != "application/octet-stream" and declared:
        return declared
    import mimetypes
    guess = mimetypes.guess_type(filename or "")[0]
    if guess in KNOWN_MIMES:
        return guess
    # container /etc/mime.types may lack office types - explicit extension map as last resort
    ext_map = {"xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
               "pdf": "application/pdf", "jpg": "image/jpeg", "jpeg": "image/jpeg",
               "png": "image/png", "webp": "image/webp", "txt": "text/plain"}
    ext = (filename or "").rsplit(".", 1)[-1].lower()
    return ext_map.get(ext, declared or "application/octet-stream")


def find_by_sha(sha):
    with db() as c, c.cursor() as cur:
        cur.execute("SELECT id, status, filename FROM documents WHERE sha256=%s "
                    "ORDER BY created LIMIT 1", (sha,))
        r = cur.fetchone()
        return {"document_id": r[0], "status": r[1], "filename": r[2]} if r else None


async def ingest_one(raw: bytes, filename: str, declared_mime: str,
                     doc_type: str, notes: str) -> dict:
    """Shared single-document pipeline: save original -> job -> extract -> validate
    -> publish (or flag). Used by /documents (multipart) and /documents/batch."""
    sha = hashlib.sha256(raw).hexdigest()
    doc_id = uuid.uuid4().hex
    mime = sniff_mime(raw, (declared_mime or "").split(";")[0], filename)
    suffix = MIME_SUFFIX.get(mime) or os.path.splitext(filename or "")[1].lstrip(".")[:5] or "bin"
    stored = UPLOAD_DIR / f"{sha[:16]}.{suffix}"
    stored.write_bytes(raw)
    with db() as c, c.cursor() as cur:
        cur.execute("INSERT INTO documents(id,filename,sha256,mime,size,stored_path,status) "
                    "VALUES(%s,%s,%s,%s,%s,%s,'UPLOADED')",
                    (doc_id, filename, sha, mime, len(raw), str(stored)))
        cur.execute("INSERT INTO jobs(document_id,kind,status) VALUES(%s,'extract','PENDING') RETURNING id",
                    (doc_id,))
        job_id = cur.fetchone()[0]
        try:
            extractor = EXTRACTORS.get(mime)
            std = extractor(raw) if extractor else {"kind": "unsupported", "error": mime}
            std["doc_type"] = doc_type or "other"
            std["notes"] = notes or ""
            env = {"schema_version": "1.0", "document_id": doc_id, "filename": filename,
                   "sha256": sha, "uploaded_at": datetime.datetime.utcnow().isoformat() + "Z",
                   "doc_type": doc_type or "other", "notes": notes or "",
                   "extracted": std}
            ok, flags = validate(env)
            status = "VALIDATED" if ok else "FLAGGED"
            cur.execute("UPDATE documents SET standard_json=%s::jsonb, validation=%s::jsonb, "
                        "status=%s, updated=now() WHERE id=%s",
                        (json.dumps(std), json.dumps({"passed": ok, "flagged_fields": flags}),
                         status, doc_id))
            cur.execute("UPDATE jobs SET status='DONE', updated=now() WHERE id=%s", (job_id,))
            task_id = None
            if ok:
                task_id = str(uuid.uuid4())
                cur.execute("INSERT INTO delivery_tasks(id,document_id,target_url,status) "
                            "VALUES(%s,%s,%s,'QUEUED')", (task_id, doc_id, TARGET_API_URL))
            c.commit()
            if ok:
                publish("deliver", {"task_id": task_id, "document_id": doc_id})
                return {"document_id": doc_id, "sha256": sha, "job_id": job_id,
                        "status": "VALIDATED", "delivery_task": task_id}
            try:
                req = urllib.request.Request(N8N_REVIEW_WEBHOOK,
                                             data=json.dumps({"document_id": doc_id, "flags": flags}).encode(),
                                             headers={"Content-Type": "application/json"}, method="POST")
                urllib.request.urlopen(req, timeout=15)
            except Exception:
                pass
            return {"document_id": doc_id, "sha256": sha, "job_id": job_id,
                    "status": "FLAGGED", "flagged_fields": flags}
        except Exception as e:
            c.rollback()
            with db() as c2, c2.cursor() as cur2:
                cur2.execute("UPDATE jobs SET status='FAILED', last_error=%s, updated=now() WHERE id=%s",
                             (str(e)[:500], job_id))
                cur2.execute("UPDATE documents SET status='EXTRACT_FAILED', updated=now() WHERE id=%s",
                             (doc_id,))
                c2.commit()
            raise HTTPException(500, f"extract failed: {e}")


def _meta_from_query(request, doc_type, notes):
    qp = request.query_params
    if not doc_type or doc_type == "undefined":
        doc_type = qp.get("doc_type", "") or ""
    if not notes or notes == "undefined":
        notes = qp.get("notes", "") or ""
    if doc_type in ("", "undefined", "null"):
        doc_type = "other"
    return doc_type, notes


@app.post("/documents")
async def upload(request: Request,
                 file: UploadFile = File(...),
                 doc_type: str = Form(""),
                 notes: str = Form("")):
    doc_type, notes = _meta_from_query(request, doc_type, notes)
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "empty file")
    result = await ingest_one(raw, file.filename, file.content_type or "", doc_type, notes)
    if os.environ.get("OCR_AUTO", "1") == "1" and result.get("document_id"):
        if result.get("status") == "FLAGGED":
            _auto_ocr_bg([result["document_id"]])
            result["auto_ocr_started"] = 1
        elif result.get("status") == "VALIDATED" and os.environ.get("MAP_AUTO", "1") == "1":
            _auto_map_bg([result["document_id"]])
            result["auto_map_started"] = 1
    return result


def _auto_map_bg(doc_ids):
    """Digital PDFs (text layer OK -> VALIDATED, skip OCR) still deserve the map:
    classify -> (full map if money_doc | light profile) -> ledger. Runs in bg thread."""
    def worker():
        for did in doc_ids:
            try:
                with db() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute("SELECT filename, standard_json FROM documents WHERE id=%s", (did,))
                    row = cur.fetchone()
                if not row:
                    continue
                std = dict(row["standard_json"] or {})
                if std.get("mapped") or std.get("classified"):
                    continue
                classify_and_route(did, std, row["filename"],
                                   explicit_invoice=(std.get("doc_type") or "").lower() == "invoice")
                with db() as c, c.cursor() as cur:
                    cur.execute("UPDATE documents SET standard_json=%s::jsonb, updated=now() WHERE id=%s",
                                (json.dumps(std), did))
                if std.get("mapped"):
                    try:
                        upsert_invoice_row(did, row["filename"], std)
                    except Exception as ue:
                        print("LEDGER upsert (digital) failed:", str(ue)[:150], flush=True)
                print("AUTO-MAP(digital)", did[:8], "->",
                      (std.get("classified") or {}).get("class"),
                      "mapped" if std.get("mapped") else "profile", flush=True)
            except Exception as e:
                print("AUTO-MAP(digital)", did[:8], "FAILED:", str(e)[:200], flush=True)
    threading.Thread(target=worker, daemon=True).start()


def _auto_ocr_bg(doc_ids):
    """Option: auto-OCR (default engine) for flagged docs, sequential background thread.
    Triggered right after /documents/batch when OCR_AUTO=1. n8n OCR workflow not required."""
    def one(did):
        r = run_ocr(did)
        print("AUTO-OCR", did[:8], "->", r.get("status"), r.get("chars"), "chars", flush=True)

    def worker():
        for did in doc_ids:
            try:
                one(did)
            except Exception as e:
                print("AUTO-OCR", did[:8], "FAILED:", str(e)[:200], "- retry in 60s", flush=True)
                try:
                    time.sleep(60)
                    one(did)
                except Exception as e2:
                    print("AUTO-OCR", did[:8], "FAILED AGAIN:", str(e2)[:200], flush=True)
    threading.Thread(target=worker, daemon=True).start()


@app.post("/documents/batch")

@app.post("/documents/batch")
async def upload_batch(request: Request):
    """Batch intake: {doc_type, notes, skip_existing=true, files:[{name,data_b64}]}
    Each file goes through the same single-document pipeline (own document/job/task);
    sha256 duplicates can be skipped -> SKIPPED_DUPLICATE."""
    body = await request.json()
    doc_type = body.get("doc_type") or "other"
    notes = body.get("notes") or ""
    # In testing you may want identical bytes ingested repeatedly:
    # set INGEST_ALLOW_DUP=1 on dp-extractor to bypass ALL dedupe regardless of payload.
    allow_dup = os.environ.get("INGEST_ALLOW_DUP", "0") == "1"
    skip = False if allow_dup else body.get("skip_existing", True)
    files = body.get("files") or []
    if not isinstance(files, list) or not files:
        raise HTTPException(400, "files: non-empty list required")
    if len(files) > 25:
        raise HTTPException(400, "batch max 25 files")
    results = []
    for f in files:
        name = (f or {}).get("name") or "unnamed"
        try:
            raw = base64.b64decode(f.get("data_b64") or "", validate=False)
        except Exception:
            results.append({"filename": name, "status": "ERROR", "error": "invalid base64"})
            continue
        if not raw:
            results.append({"filename": name, "status": "ERROR", "error": "empty file"})
            continue
        sha = hashlib.sha256(raw).hexdigest()
        if skip:
            dup = find_by_sha(sha)
            if dup:
                results.append({"filename": name, "sha256": sha, "status": "SKIPPED_DUPLICATE",
                                "document_id": dup["document_id"], "existing_status": dup["status"]})
                continue
        try:
            r = await ingest_one(raw, name, f.get("mime") or "", doc_type, notes)
        except HTTPException as e:
            r = {"filename": name, "sha256": sha, "status": "EXTRACT_FAILED", "error": str(e.detail)[:200]}
        r["filename"] = name
        results.append(r)
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    # Auto-chain: form upload -> FLAGGED docs get OCR'd immediately (default engine
    # from settings), sequential background thread. OCR_AUTO=0 disables.
    if os.environ.get("OCR_AUTO", "1") == "1" and "auto_ocr" not in body:
        flagged = [r["document_id"] for r in results if r.get("status") == "FLAGGED"]
        validated = [r["document_id"] for r in results if r.get("status") == "VALIDATED"]
        if flagged:
            _auto_ocr_bg(flagged)
        if validated and os.environ.get("MAP_AUTO", "1") == "1":
            _auto_map_bg(validated)
        return {"batch": True, "total": len(results), "summary": counts,
                "auto_ocr_started": len(flagged), "auto_map_started": len(validated), "results": results}
    return {"batch": True, "total": len(results), "summary": counts, "results": results}


@app.post("/documents/{doc_id}/correct")
async def correct_fields(doc_id: str, request: Request):
    """Review loop entry: n8n/user posts corrected extracted fields; re-validate;
    on pass, publish deliver (the 'No -> review -> back' arrow)."""
    body = await request.json()
    with db() as c, c.cursor() as cur:
        cur.execute("SELECT standard_json,filename,sha256,status,review_loop FROM documents WHERE id=%s", (doc_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "document not found")
        std = body.get("extracted", row[0])
        env = {"schema_version": "1.0", "document_id": doc_id, "filename": row[1],
               "sha256": row[2], "extracted": std}
        ok, flags = validate(env)
        cur.execute("UPDATE documents SET standard_json=%s::jsonb, validation=%s::jsonb, "
                    "status=%s, review_loop=review_loop+1, updated=now() WHERE id=%s",
                    (json.dumps(std), json.dumps({"passed": ok, "flagged_fields": flags}),
                     "VALIDATED" if ok else "FLAGGED", doc_id))
        task_id = None
        if ok:
            task_id = str(uuid.uuid4())
            cur.execute("INSERT INTO delivery_tasks(id,document_id,target_url,status) "
                        "VALUES(%s,%s,%s,'QUEUED')", (task_id, doc_id, TARGET_API_URL))
        c.commit()
        if ok:
            publish("deliver", {"task_id": task_id, "document_id": doc_id})
    return {"document_id": doc_id, "validated": ok, "delivery_task": task_id, "flags": flags}


OCR_MAX_PAGES = int(os.environ.get("OCR_MAX_PAGES", "30"))
OCR_DPI = os.environ.get("OCR_DPI", "200")

# --- Runtime OCR config (UI-editable, hot-reload by mtime; env = bootstrap only) ---
# Design: DESIGN_OCR_SETTINGS.md. Precedence: /data/ocr_config.json > env > defaults.
OCR_CONFIG_PATH = Path(os.environ.get("OCR_CONFIG_PATH", "/data/ocr_config.json"))
_ocr_cache = {"mtime": None, "cfg": None}

DEFAULT_QWEN_URL = ("https://" + "token-plan.ap-southeast-1.maas.aliyuncs.com"
                    + "/compatible-mode/v1/chat/completions")

def _env_ocr_defaults():
    return {
        "endpoint": os.environ.get("QWEN_URL") or DEFAULT_QWEN_URL,
        "model": os.environ.get("QWEN_MODEL", "qwen3.8-flash"),
        "key": os.environ.get("QWEN_API_KEY", ""),
        "engine": os.environ.get("OCR_ENGINE", "qwen"),
    }

def load_ocr_config():
    """mtime-cached: a UI save is live on the very next request, no restart."""
    try:
        mt = OCR_CONFIG_PATH.stat().st_mtime
    except OSError:
        mt = None
    if mt is None:
        cfg = _env_ocr_defaults()
    elif _ocr_cache["mtime"] != mt:
        try:
            cfg = json.loads(OCR_CONFIG_PATH.read_text())
        except Exception:
            cfg = _env_ocr_defaults()
        _ocr_cache.update(mtime=mt, cfg=cfg)
    else:
        cfg = _ocr_cache["cfg"]
    base = _env_ocr_defaults()
    for k in base:
        cfg.setdefault(k, base[k])
    return cfg

def normalize_chat_url(url):
    """Accept base or full URL; return chat-completions endpoint."""
    url = (url or "").strip().rstrip("/")
    if not url:
        return url
    if url.endswith("/chat/completions"):
        return url
    if url.endswith("/v1") or url.endswith("/compatible-mode"):
        return url + "/chat/completions"
    return url + "/v1/chat/completions"

def save_ocr_config(cfg):
    cfg = {k: v for k, v in cfg.items() if k in ("endpoint", "model", "key", "engine")}
    cfg["endpoint"] = normalize_chat_url(cfg.get("endpoint", ""))
    OCR_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = OCR_CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=1))
    os.chmod(tmp, 0o600)
    os.replace(tmp, OCR_CONFIG_PATH)
    _ocr_cache["mtime"] = None  # force reload
    return cfg

def mask_key(k):
    k = k or ""
    if len(k) <= 8:
        return "****" if k else "(empty)"
    import hashlib as _h
    return k[:4] + "…" + k[-4:] + f" (len {len(k)}, sha8 {_h.sha256(k.encode()).hexdigest()[:8]})"

DEFAULT_OCR_ENGINE = None  # superseded by load_ocr_config()['engine']
QWEN_KEY = None  # superseded by load_ocr_config()['key']
def api_vision_ocr(png_bytes: bytes, cfg=None) -> str:
    """Provider-agnostic vision OCR (OpenAI-compatible). cfg from UI config."""
    import urllib.request as ur
    cfg = cfg or load_ocr_config()
    if not cfg.get("key"):
        raise HTTPException(500, "OCR API key not configured — set it at /view/settings")
    body = json.dumps({
        "model": cfg["model"], "max_tokens": 1800,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(png_bytes).decode()}},
            {"type": "text", "text": "Extract ALL text from this document image exactly as written, preserving line structure. Include printed AND handwritten content. Output only the text."}]}]}).encode()
    req = ur.Request(cfg["endpoint"], data=body, method="POST")
    req.add_header("Authorization", "Bearer " + cfg["key"])
    req.add_header("Content-Type", "application/json")
    with ur.urlopen(req, timeout=300) as r:
        d = json.load(r)
    t = d["choices"][0]["message"]["content"]
    if isinstance(t, list):
        t = "\n".join(x.get("text", "") for x in t)
    return (t or "").strip()

# backwards-compat alias (older code paths)
qwen_vision_ocr = api_vision_ocr


def _tesseract(png_path: str, lang: str = "eng") -> str:
    import subprocess
    r = subprocess.run(["tesseract", png_path, "stdout", "-l", lang, "--psm", "3"],
                       capture_output=True, text=True, timeout=120)
    return r.stdout



# ---------------- structured table mapping (invoice normalization) ----------------
INVOICE_MAP_PROMPT = """Anda mesin ekstraksi data invoice untuk dokumen vendor Indonesia (cetakan, scan, foto, tulisan tangan; banyak vendor; kualitas campuran).
Dari TEKS di bawah, hasilkan SATU objek JSON sesuai skema persis ini:
{"vendor": {"name": "", "npwp": "", "address": ""},
 "invoice": {"number": "", "number_source": "printed|handwritten|stamp|inferred|null", "number_confidence": "high|medium|low", "date": "YYYY-MM-DD atau null", "ref_po": "", "doc_type_label": "mis. FAKTUR PAJAK/INVOICE/DELIVERY ORDER"},
 "amounts": {"currency": "IDR", "subtotal": null, "tax": null, "total": null, "as_written": "nilai total persis seperti tertulis"},
 "line_items": [{"description": "", "code": "", "qty": null, "uom": "", "unit_price": null, "amount": null, "page": null}],
 "handwritten": [{"content": "transkripsi verbatim", "interpreted": "makna jika jelas (tanggal, nopol, tanda tangan, LUNAS, paraf)", "page": null}],
 "payments": {"bank": "", "account": "", "account_name": ""},
 "confidence": "high|medium|low", "missing": ["field yang tidak ditemukan"], "notes": "hal yang meragukan + alasan pilihan angka ambigu", "others": "detail penting yang terlihat tetapi belum terwakili oleh field di atas; null jika tidak ada"}
ATURAN:
- Rupiah: "Rp 1.234.567,89" -> 1234567.89 (titik=ribuan, koma=desimal). Jika ambigu, pilih yang masuk akal secara pembukuan lalu turunkan confidence dan tulis di notes.
- Nomor invoice bisa di mana saja: header (No./Faktur/Invoice), cap/stempel, coretan/lingkaran, tulisan tangan. Jika tidak eksplisit -> null dan daftarkan "invoice_number" di missing; JANGAN mengarang.
- SEMUA konten tulisan tangan wajib masuk handwritten, meskipun juga ada di teks cetak.
- amounts berupa angka murni tanpa Rp/titik/koma. field string kosong pakai null.
- BATASI OUTPUT: line_items maksimal 10 baris paling penting; jika lebih, tulis total baris + agregat
  (mis. qty gabungan) di notes dan ringkas sisanya SATU baris "lain-lain". handwritten maksimal 15.
- Dokumen multi-halaman: fokus halaman header invoice & total; lampiran cukup diringkas.
Keluarkan HANYA JSON, tanpa markdown, tanpa penjelasan."""


def llm_text(prompt: str, cfg, max_tokens: int = 4000) -> str:
    import urllib.request as ur
    body = json.dumps({"model": cfg["model"], "max_tokens": max_tokens,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = ur.Request(cfg["endpoint"], data=body, method="POST")
    req.add_header("Authorization", "Bearer " + cfg["key"])
    req.add_header("Content-Type", "application/json")
    with ur.urlopen(req, timeout=int(os.environ.get("MAP_TIMEOUT", "600"))) as r:
        d = json.load(r)
    t = d["choices"][0]["message"]["content"]
    if isinstance(t, list):
        t = "\n".join(x.get("text", "") for x in t)
    return t or ""


def _parse_json_loose(txt: str):
    txt = txt.strip()
    if txt.startswith("```"):
        txt = txt.strip("`")
        txt = txt[txt.find("{"):]
    try:
        return json.loads(txt)
    except Exception:
        i, j = txt.find("{"), txt.rfind("}")
        if i >= 0 and j > i:
            return json.loads(txt[i:j + 1])
        raise


CLASSIFY_PROMPT = """Anda pengklasifikasi dokumen bisnis Indonesia. Dari potongan teks + nama file, tentukan:
{
 "class": "invoice|faktur_penjualan|faktur_pajak|delivery_order|sales_order|purchase_order|work_order|surat_jalan|tanda_terima|kwitansi|bukti_transfer|bon_transfer|credit_note|kontrak|moU|surat_resmi|nota_retil|struk|laporan|proposal|lainnya",
 "money_doc": true/false,   // mengandung tagihan/nilai uang yang perlu masuk ledger penagihan
 "confidence": "high|medium|low",
 "title": "judul/label persis pada dokumen (max 80 char)",
 "number": "nomor dokumen (surat/DO/SO/PO/faktur) atau null",
 "date": "YYYY-MM-DD atau null",
 "issuer": "pihak penerbit/di kiri atas atau null",
 "recipient": "penerima addressed-to atau null",
 "amount": angka rupiah murni atau null,
 "notes": "1 kalimat: dasar klasifikasi; hal penting bila money_doc=false",
 "others": "detail penting yang belum punya field terstruktur; null jika tidak ada"
}
Aturan: FAKTUR PENJUALAN/DELIVERY ORDER yang berisi barang+qty TERBIT PENJUAL = money_doc true (nanti di-map penuh).
Bukti transfer/kwitansi/credit note = money_doc true. Work order/sales order/PO tanpa harga = false; jika ada nilai kontrak/tagihan, boleh true tetapi jangan mengarang invoice number. Kontrak/surat jalan/tanda terima tanpa harga/proposal/laporan = false.
Jangan mengarang; null lebih baik. HANYA JSON."""

def classify_document(std: dict, filename: str = "") -> dict:
    """One small, cheap LLM call: what kind of document is this?"""
    if std.get("kind") == "pdf":
        pages = std.get("pages") or []
        text = "\n".join((p.get("text") or "") for p in pages[:2])
        if len(pages) > 2:
            text += "\n...\n" + (pages[-1].get("text") or "")
    else:
        text = std.get("ocr") or std.get("text") or ""
    if not text.strip():
        return {"class": "lainnya", "money_doc": False, "confidence": "low", "error": "no text to classify"}
    prompt = CLASSIFY_PROMPT + f"\n\nNAMA FILE: {filename}\n\n=== POTONGAN TEKS ===\n" + text[:2500]
    raw = llm_text(prompt, load_ocr_config(), max_tokens=500)
    out = raw.strip()
    if out.startswith("```"):
        out = out.split("```")[1].strip().lstrip("json").strip()
    try:
        c = json.loads(out)
    except Exception:
        raise HTTPException(502, "classifier output not JSON: " + out[:200])
    c["_classifier"] = {"model": load_ocr_config().get("model"), "source_chars": len(text)}
    c.setdefault("money_doc", False)
    return c


def profile_to_row(doc_id: str, filename: str, cls: dict):
    """Non-money document -> one light ledger row (identity only)."""
    sql = """INSERT INTO invoice_rows (document_id, filename, vendor_name, doc_type_label, invoice_number,
        invoice_date, currency, total, notes, others, confidence, mapper_model, doc_class, money_doc, status)
        VALUES (%s,%s,%s,%s,%s,NULLIF(%s,'')::date,NULL,NULLIF(%s,'')::numeric,%s,%s,%s,%s,%s,false,'extracted')
        ON CONFLICT (document_id) DO UPDATE SET
          filename=EXCLUDED.filename, vendor_name=EXCLUDED.vendor_name, doc_type_label=EXCLUDED.doc_type_label,
          invoice_number=EXCLUDED.invoice_number, invoice_date=EXCLUDED.invoice_date, total=EXCLUDED.total,
          notes=EXCLUDED.notes, others=EXCLUDED.others, confidence=EXCLUDED.confidence, mapper_model=EXCLUDED.mapper_model,
          doc_class=EXCLUDED.doc_class, money_doc=false, updated_at=now()"""
    vals = (doc_id, filename, cls.get("issuer"), cls.get("title"), cls.get("number"),
            cls.get("date"), (cls.get("amount") if str(cls.get("amount") or "").strip() else None),
            ("penerima: %s. " % cls.get("recipient") if cls.get("recipient") else "") + (cls.get("notes") or "")[:400],
            (cls.get("others") or "")[:4000], cls.get("confidence"),
            (cls.get("_classifier") or {}).get("model"), cls.get("class"))
    with db() as c, c.cursor() as cur:
        cur.execute(sql, vals)


def classify_and_route(doc_id: str, std: dict, filename: str, explicit_invoice: bool = False):
    """MAP_AUTO router: classify anything -> money docs full-map, others light-profile."""
    if explicit_invoice:
        std["mapped"] = map_std_to_table(std)
        std["others"] = std["mapped"].get("others") or std["mapped"].get("notes")
        std["classified"] = {"class": (std["mapped"].get("invoice") or {}).get("doc_type_label") or "invoice",
                             "money_doc": True, "forced_by": "doc_type=invoice"}
        return
    cls = classify_document(std, filename)
    std["classified"] = cls
    if cls.get("money_doc"):
        std["mapped"] = map_std_to_table(std)
        std["others"] = std["mapped"].get("others") or std["mapped"].get("notes")
    else:
        std["others"] = cls.get("others") or cls.get("notes")
        cls["others"] = std["others"]
        try:
            profile_to_row(doc_id, filename, cls)
            print("PROFILE-ROW", doc_id[:8], cls.get("class"), flush=True)
        except Exception as pe:
            print("PROFILE-ROW failed:", str(pe)[:150], flush=True)



def map_std_to_table(std: dict, kind: str = "invoice") -> dict:
    """LLM-normalize extracted/OCR'd text into the structured vendor table row."""
    cfg = load_ocr_config()
    if not cfg.get("key"):
        raise HTTPException(500, "OCR API key not configured — set it at /view/settings")
    if std.get("kind") == "pdf":
        pages = std.get("pages", [])
        # Multi-page invoices: model generation, not reading, is the bottleneck.
        # Keep header pages (1-2) + last page (grand total); summarize the middle.
        if len(pages) > 3:
            keep = pages[:2] + pages[-1:]
            text = "\n".join(f"--- HALAMAN {p.get('page')} ---\n{p.get('text', '')}" for p in keep)
            text = (f"[DOKUMEN {len(pages)} HALAMAN: halaman 1-2 & terakhir dikirim lengkap; "
                    f"halaman {3}-{len(pages)-1} diringkas 300 karakter pertama per halaman.]\n"
                    + "\n".join(f"--- HALAMAN {p.get('page')} (ringkas) ---\n{(p.get('text') or '')[:300]}"
                                 for p in pages[2:-1]) + "\n\n" + text)
        else:
            text = "\n".join(f"--- HALAMAN {p.get('page')} ---\n{p.get('text', '')}" for p in pages)
    else:
        text = std.get("ocr") or std.get("text") or ""
    if len(text.strip()) < 10:
        raise HTTPException(422, "nothing to map: document text still empty — run OCR first (engine=qwen)")
    prompt = INVOICE_MAP_PROMPT + "\n\n=== TEKS DOKUMEN ===\n" + text[:14000]
    raw = llm_text(prompt, cfg)
    out = _parse_json_loose(raw)
    out["_mapper"] = {"model": cfg["model"], "source_chars": len(text)}
    return out


# ---- invoice_rows: standard vendor-invoice ledger (v1.5) --------------------
# Schema lives ONLY in db-init/03-invoice-rows.sql (migrations, not app code).
# Fresh machine: postgres creates it automatically (docker-entrypoint-initdb.d).
# Existing DB (created before v1.5): apply once —
#   docker exec -i dp-db psql -U pipeline -d pipeline < db-init/03-invoice-rows.sql


def upsert_invoice_row(doc_id, filename, std):
    """After auto-map: write/refresh the standard ledger row (keeps review columns
    when re-extracting an existing doc)."""
    m = std.get("mapped") or {}
    if not m or m.get("error"):
        return None
    inv, ven, amt = m.get("invoice") or {}, m.get("vendor") or {}, m.get("amounts") or {}
    def _num(v):
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        try:
            return float(str(v).replace(",", ""))
        except Exception:
            return None
    cols = ["document_id","filename","vendor_name","vendor_npwp","vendor_address","doc_type_label",
            "invoice_number","invoice_number_source","invoice_number_confidence","invoice_date",
            "ref_po","currency","subtotal","tax","total","total_as_written","line_items","handwritten",
            "confidence","missing","notes","others","mapper_model","doc_class"]
    vals = (doc_id, filename, ven.get("name"), ven.get("npwp"), ven.get("address"),
            inv.get("doc_type_label"), inv.get("number"), inv.get("number_source"),
            inv.get("number_confidence"), inv.get("date"),
            inv.get("ref_po"), amt.get("currency") or "IDR",
            _num(amt.get("subtotal")), _num(amt.get("tax")), _num(amt.get("total")), amt.get("as_written"),
            json.dumps(m.get("line_items") or []), json.dumps(m.get("handwritten") or []),
            m.get("confidence"), ";".join(m.get("missing") or []), (m.get("notes") or "")[:1000],
            (m.get("others") or std.get("others") or "")[:4000],
            (m.get("_mapper") or {}).get("model"),
            (std.get("classified") or {}).get("class") or inv.get("doc_type_label"))
    ph = ["%s"] * len(cols)
    ph[9] = "NULLIF(%s,'')::date"       # invoice_date
    # subtotal/tax/total already coerced to float-or-None by _num — bind directly
    ph[16] = "%s::jsonb"; ph[17] = "%s::jsonb"
    upd = [f"{c}=EXCLUDED.{c}" for c in cols if c != "document_id"]
    sql = (f"INSERT INTO invoice_rows ({', '.join(cols)}, mapped_at, status, money_doc, updated_at) "
           f"VALUES ({', '.join(ph)}, now(), 'extracted', true, now()) "
           f"ON CONFLICT (document_id) DO UPDATE SET {', '.join(upd)}, money_doc=true, mapped_at=now(), updated_at=now()")
    assert len(vals) == len(cols), f"cols {len(cols)} vs vals {len(vals)}"
    with db() as c, c.cursor() as cur:
        cur.execute(sql, vals)
    return True


@app.get("/invoices")
def list_invoices(status: str = "", limit: int = 200):
    """Standard vendor-invoice table — RPA pulls here (e.g. ?status=extracted)."""
    lim = min(max(1, limit), 1000)
    with db() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        if status:
            cur.execute("SELECT * FROM invoice_rows WHERE status=%s ORDER BY invoice_date NULLS LAST, id DESC LIMIT %s", (status, lim))
        else:
            cur.execute("SELECT * FROM invoice_rows ORDER BY id DESC LIMIT %s", (lim,))
        rows = [dict(r) for r in cur.fetchall()]
    return {"count": len(rows), "rows": rows}


@app.get("/invoices/export.csv")
def invoices_csv(status: str = ""):
    import csv as _csv
    cols = ["document_id","filename","vendor_name","vendor_npwp","invoice_number","invoice_number_source",
            "invoice_date","ref_po","doc_type_label","currency","subtotal","tax","total","total_as_written",
            "confidence","missing","others","rpa_vendor","rpa_invoice_number","rpa_date","rpa_total","rpa_status","rpa_note",
            "status","mapped_at"]
    with db() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM invoice_rows" + (" WHERE status=%s" % status if status else "") + " ORDER BY id")
        rows = [dict(r) for r in cur.fetchall()]
    buf = io.StringIO()
    w = _csv.writer(buf); w.writerow(cols)
    for r in rows:
        w.writerow([r.get(k) for k in cols])
    return Response(content=buf.getvalue(), media_type="text/csv")


@app.patch("/invoices/{doc_id}")
async def patch_invoice(doc_id: str, request: Request):
    """RPA/review write-back: set workflow status and/or final ledger values.
    {"status":"pending_review|mapped", "rpa_vendor":..., "rpa_invoice_number":...,
     "rpa_date":"YYYY-MM-DD", "rpa_total":123456.78, "rpa_note":"...", "rpa_status":"ok|corrected|rejected"}"""
    body = await request.json()
    sets, vals = [], []
    for k in ("status", "rpa_vendor", "rpa_invoice_number", "rpa_note", "rpa_status"):
        if k in body:
            sets.append(f"{k}=%s"); vals.append(body[k])
    if "rpa_date" in body:
        sets.append("rpa_date=NULLIF(%s,'')::date"); vals.append(body["rpa_date"])
    if "rpa_total" in body:
        sets.append("rpa_total=%s"); vals.append(body["rpa_total"])
    if body.get("rpa_status") is not None:
        sets.append("rpa_reviewed_at=now()")
    if not sets:
        raise HTTPException(400, "nothing to update")
    vals.append(doc_id)
    with db() as c, c.cursor() as cur:
        cur.execute(f"UPDATE invoice_rows SET {', '.join(sets)}, updated_at=now() WHERE document_id=%s RETURNING id", vals)
        if not cur.fetchone():
            raise HTTPException(404, "invoice row not found")
    return {"ok": True, "document_id": doc_id}


@app.get("/documents/{doc_id}/mapped.csv")
def mapped_csv(doc_id: str):
    """One CSV row per mapped document — drop straight into the vendor invoice table."""
    import csv as _csv
    with db() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT standard_json, filename FROM documents WHERE id=%s", (doc_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(404, "document not found")
    m = (row["standard_json"] or {}).get("mapped") or {}
    if m.get("error") or not m:
        raise HTTPException(422, "no mapping yet — POST /documents/{}/map or OCR with doc_type=invoice")
    inv, ven, amt = m.get("invoice") or {}, m.get("vendor") or {}, m.get("amounts") or {}
    cols = ["filename","document_id","vendor_name","vendor_npwp","vendor_address","invoice_number",
            "invoice_number_source","invoice_number_confidence","invoice_date","ref_po","doc_type_label",
            "subtotal","tax","total","as_written","line_item_count","handwritten_count",
            "confidence","missing","others","notes"]
    buf = io.StringIO()
    w = _csv.writer(buf)
    w.writerow(cols)
    hw = m.get("handwritten") or []
    li = m.get("line_items") or []
    w.writerow([row["filename"], doc_id, ven.get("name"), ven.get("npwp"), ven.get("address"),
                inv.get("number"), inv.get("number_source"), inv.get("number_confidence"),
                inv.get("date"), inv.get("ref_po"), inv.get("doc_type_label"),
                amt.get("subtotal"), amt.get("tax"), amt.get("total"), amt.get("as_written"),
                len(li), len(hw), m.get("confidence"),
                ";".join(m.get("missing") or []), (m.get("others") or std.get("others") or "").replace("\n", " ")[:4000],
                (m.get("notes") or "").replace("\n", " ")[:400]])
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=mapped_{doc_id[:8]}.csv"})


@app.post("/documents/{doc_id}/map")
async def map_doc(doc_id: str, request: Request):
    """Map a document's text into the structured invoice table row (manual/other types)."""
    kind = "invoice"
    try:
        kind = ((await request.json()) or {}).get("doc_type", "invoice") or "invoice"
    except Exception:
        pass
    with db() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT standard_json FROM documents WHERE id=%s", (doc_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "document not found")
        std = dict(row["standard_json"] or {})
        mapped = map_std_to_table(std, kind)
        std["mapped"] = mapped
        cur.execute("UPDATE documents SET standard_json=%s::jsonb, updated=now() WHERE id=%s",
                    (json.dumps(std), doc_id))
    try:
        upsert_invoice_row(doc_id, (get_document(doc_id) or {}).get("filename", ""), std)
    except Exception as ue:
        print("LEDGER upsert (manual) failed:", str(ue)[:150], flush=True)
    return {"document_id": doc_id, "mapped": mapped}


@app.post("/documents/{doc_id}/ocr")
def run_ocr(doc_id: str, lang: str = "eng", engine: str = ""):
    """OCR the STORED original (pdf-scan or image), merge into standard_json,
    re-validate; on pass publish delivery like /correct.
    engine: 'qwen' (default, API — qwen3.8-flash via QWEN_MODEL) or 'tesseract' (local)."""
    engine = engine or load_ocr_config()["engine"]
    with db() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM documents WHERE id=%s", (doc_id,))
        d = cur.fetchone()
        if not d:
            raise HTTPException(404, "document not found")
        path = d["stored_path"]
        if not os.path.exists(path):
            raise HTTPException(410, "stored original file missing")
        mime = d["mime"]
        std = dict(d["standard_json"] or {})
        pages_text = []
        ocr_cfg = load_ocr_config() if engine == "qwen" else None
        if engine == "qwen" and not ocr_cfg.get("key"):
            raise HTTPException(500, "OCR API key not configured — set it at /view/settings")
        try:
            if mime == "application/pdf":
                info = subprocess.run(["pdfinfo", path], capture_output=True, text=True).stdout
                n = OCR_MAX_PAGES
                for ln in info.splitlines():
                    if ln.startswith("Pages"):
                        n = min(OCR_MAX_PAGES, int(ln.split(":")[-1].strip()))
                with tempfile.TemporaryDirectory() as td:
                    prefix = os.path.join(td, "pg")
                    subprocess.run(["pdftoppm", "-png", "-r", OCR_DPI, "-f", "1", "-l", str(n), path, prefix],
                                   check=True, timeout=300)
                    pngs = sorted(glob.glob(prefix + "*.png"))
                    for i, pg in enumerate(pngs, 1):
                        if engine == "qwen":
                            with open(pg, "rb") as fh:
                                txt = api_vision_ocr(fh.read(), ocr_cfg)
                        else:
                            txt = _tesseract(pg, lang)
                        pages_text.append({"page": i, "text": txt})
                std["kind"] = "pdf"
                std["ocr_engine"] = engine
                if engine == "qwen": std["ocr_model"] = ocr_cfg["model"]
            elif mime in ("image/jpeg", "image/png", "image/webp"):
                if engine == "qwen":
                    with open(path, "rb") as fh:
                        img_txt = api_vision_ocr(fh.read(), ocr_cfg)
                else:
                    img_txt = _tesseract(path, lang)
                pages_text = [{"page": 1, "text": img_txt}]
                std["kind"] = "image"
                std["ocr_engine"] = engine
                if engine == "qwen": std["ocr_model"] = ocr_cfg["model"]
            else:
                raise HTTPException(400, f"mime not OCR-able: {mime}")
        except subprocess.TimeoutExpired:
            raise HTTPException(504, "OCR timeout")

        # merge OCR text into the document content.
        # If the existing page text came from a PREVIOUS OCR run (ocr_engine set),
        # the new engine's output REPLACES it wholesale; otherwise keep the richer
        # of native-text-layer vs OCR result per page.
        prev_was_ocr = bool(std.get("ocr_engine"))
        if std.get("kind") == "pdf":
            merged = []
            base_pages = {p["page"]: p.get("text", "") for p in std.get("pages", [])}
            for p in pages_text:
                t = base_pages.get(p["page"], "")
                if prev_was_ocr:
                    keep = p["text"].strip()
                else:
                    keep = (t + "\n" + p["text"]).strip() if len(t.strip()) < len(p["text"].strip()) else t.strip()
                merged.append({"page": p["page"], "text": keep})
            std["pages"] = merged
            std["page_count"] = max(std.get("page_count", 0), len(merged))
        else:
            std["ocr"] = pages_text[0]["text"]

        # Auto-map: invoice docs get the normalized vendor-table row as part of OCR.
        if os.environ.get("MAP_AUTO", "1") == "1":
            try:
                classify_and_route(doc_id, std, d["filename"],
                                   explicit_invoice=(std.get("doc_type") or "").lower() == "invoice")
                if std.get("mapped"):
                    try:
                        upsert_invoice_row(doc_id, d["filename"], std)
                    except Exception as ue:
                        print("LEDGER upsert failed:", str(ue)[:150], flush=True)
                    print("AUTO-MAP", doc_id[:8], "ok conf:", std["mapped"].get("confidence"), flush=True)
                else:
                    print("AUTO-CLASSIFY", doc_id[:8], "->", (std.get("classified") or {}).get("class"), "(non-money)", flush=True)
            except Exception as e:
                std["mapped"] = {"error": str(e)[:300]}
                print("AUTO-MAP", doc_id[:8], "FAILED:", str(e)[:200], flush=True)
        env = {"schema_version": std.get("schema_version", "1.0"), "document_id": doc_id,
               "filename": d["filename"], "sha256": d["sha256"], "extracted": std}
        ok, flags = validate(env)
        cur.execute("UPDATE documents SET standard_json=%s::jsonb, validation=%s::jsonb, "
                    "status=%s, review_loop=review_loop+1, updated=now() WHERE id=%s",
                    (json.dumps(std), json.dumps({"passed": ok, "flagged_fields": flags}),
                     "VALIDATED" if ok else "FLAGGED", doc_id))
        task_id = None
        if ok:
            task_id = str(uuid.uuid4())
            cur.execute("INSERT INTO delivery_tasks(id,document_id,target_url,status) "
                        "VALUES(%s,%s,%s,'QUEUED')", (task_id, doc_id, TARGET_API_URL))
        c.commit()
        if ok:
            publish("deliver", {"task_id": task_id, "document_id": doc_id})
        total_chars = sum(len(p["text"]) for p in std.get("pages", pages_text)) if std.get("kind") == "pdf" else len(std.get("ocr", ""))
        return {"document_id": doc_id, "engine": engine, "pages_ocr": len(pages_text),
                "chars": total_chars, "validated": ok, "flags": flags, "delivery_task": task_id}


@app.get("/documents/{doc_id}/raw")
def get_raw(doc_id: str, b64: int = 0):
    with db() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT stored_path,mime,filename FROM documents WHERE id=%s", (doc_id,))
        d = cur.fetchone()
    if not d or not os.path.exists(d["stored_path"]):
        raise HTTPException(404, "file not found")
    if b64:
        with open(d["stored_path"], "rb") as fh:
            return {"filename": d["filename"], "mime": d["mime"],
                    "b64": base64.b64encode(fh.read()).decode()}
    return FileResponse(d["stored_path"], media_type=d["mime"] or "application/octet-stream",
                        filename=d["filename"])


@app.get("/documents")
def list_documents(limit: int = 50):
    with db() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT id,filename,mime,size,status,review_loop,created,updated "
                    "FROM documents ORDER BY created DESC LIMIT %s", (limit,))
        return {"documents": [dict(r) for r in cur.fetchall()]}


@app.get("/documents/{doc_id}")
def get_document(doc_id: str):
    with db() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM documents WHERE id=%s", (doc_id,))
        d = cur.fetchone()
        if not d:
            raise HTTPException(404, "not found")
        cur.execute("SELECT id,kind,status,attempts,last_error FROM jobs WHERE document_id=%s", (doc_id,))
        d["jobs"] = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT id,status,attempts,last_error,delivered_at,target_url FROM delivery_tasks WHERE document_id=%s", (doc_id,))
        d["delivery_tasks"] = [dict(r) for r in cur.fetchall()]
    return d


# ---- human-readable HTML views (browser results) ----
from fastapi.responses import HTMLResponse, RedirectResponse
import html as H

_CSS = """
body{font-family:system-ui,-apple-system,sans-serif;margin:0;background:#f6f7f9;color:#1a2233}
.wrap{max-width:980px;margin:24px auto;padding:0 16px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:22px 0 8px;color:#3c4a63}
a{color:#2456c9;text-decoration:none}
.sub{color:#68738a;font-size:13px;margin-bottom:18px}
table{border-collapse:collapse;width:100%;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 2px rgba(20,30,55,.08);margin-bottom:8px}
th,td{text-align:left;padding:8px 12px;font-size:13px;border-bottom:1px solid #e8ebf1;vertical-align:top}
th{background:#eef1f6;color:#4a5570;font-weight:600}
code{font-family:ui-monospace,monospace;font-size:12px;background:#eef1f6;padding:1px 5px;border-radius:4px}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;font-size:11px;font-weight:700;letter-spacing:.3px}
.b-DELIVERED{background:#e3f6e9;color:#1a7f3c}.b-VALIDATED{background:#e5eefc;color:#2456c9}
.b-FLAGGED{background:#fdf3d7;color:#9a6b00}.b-RETRY_WAIT{background:#fdeadd;color:#b4530a}
.b-FAILED{background:#fde3e3;color:#b32323}.b-DELIVERY_FAILED{background:#fde3e3;color:#b32323}
.b-UPLOADED,.b-QUEUED,.b-DONE,.b-PENDING{background:#eceff4;color:#5b6478}
pre{background:#0f172a;color:#d7e2f2;padding:12px;border-radius:8px;font-size:12px;overflow:auto;line-height:1.45}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px;margin:14px 0}
.kv{background:#fff;border-radius:8px;padding:10px 14px;box-shadow:0 1px 2px rgba(20,30,55,.08)}
.kv .k{font-size:11px;color:#68738a;text-transform:uppercase;letter-spacing:.4px}
.kv .v{font-size:14px;font-weight:600;margin-top:2px;word-break:break-all}
.nav{font-size:13px;margin-bottom:14px}
"""


def _badge(s):
    s = str(s)
    return f'<span class="badge b-{H.escape(s)}">{H.escape(s)}</span>'


def _page(title, body):
    return ("<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{H.escape(title)}</title><style>{_CSS}</style></head>"
            f"<body><div class='wrap'>{body}</div></body></html>")


def _render_std(std):
    out = []
    for sheet in (std.get("sheets") or []):
        head = "".join(f"<th>{H.escape(str(c))}</th>" for c in (sheet.get("columns") or []))
        body = "".join("<tr>" + "".join(f"<td>{H.escape(str(c))}</td>" for c in r) + "</tr>"
                       for r in (sheet.get("rows_preview") or []))
        out.append(f"<h2>Sheet: {H.escape(str(sheet.get('name','')))} — {sheet.get('row_count',0)} rows</h2>"
                   f"<table><tr>{head}</tr>{body}</table>")
    for p in (std.get("pages") or []):
        out.append(f"<h2>Page {p.get('page')}</h2><pre>{H.escape(p.get('text') or '(no text)')}</pre>")
    if std.get("ocr"):
        out.append(f"<h2>OCR</h2><pre>{H.escape(str(std['ocr']))}</pre>")
    m = std.get("mapped")
    if isinstance(m, dict) and m and not m.get("error"):
        esc = lambda v: H.escape(str(v)) if v not in (None, "") else "—"
        out.append("<h2>Mapped invoice row</h2>")
        out.append("<table><tr>" + "".join(f"<th>{c}</th>" for c in
                   ("Vendor","NPWP","Alamat","Invoice No","Sumber no.","Conf. no.","Date","Ref PO","Subtotal","PPN/Tax","Total","As written","Confidence")) + "</tr><tr>")
        ven, inv, amt = (m.get("vendor") or {}), (m.get("invoice") or {}), (m.get("amounts") or {})
        for c in (ven.get("name"), ven.get("npwp"), ven.get("address"), inv.get("number"),
                  inv.get("number_source"), inv.get("number_confidence"), inv.get("date"), inv.get("ref_po"),
                  amt.get("subtotal"), amt.get("tax"), amt.get("total"), amt.get("as_written"), m.get("confidence")):
            out.append(f"<td>{esc(c)}</td>")
        out.append("</tr></table>")
        li = m.get("line_items") or []
        if li:
            out.append(f"<h2>Line items ({len(li)})</h2><table><tr>" + "".join(f"<th>{c}</th>" for c in
                       ("Code","Description","Qty","UoM","Unit price","Amount","Pg")) + "</tr>")
            for x in li:
                out.append("<tr>" + "".join(f"<td>{esc(x.get(k))}</td>" for k in
                           ("code","description","qty","uom","unit_price","amount","page")) + "</tr>")
            out.append("</table>")
        hw = m.get("handwritten") or []
        if hw:
            out.append(f"<h2>Handwritten ({len(hw)})</h2><table><tr><th>Transcription</th><th>Interpretation</th><th>Pg</th></tr>")
            for x in hw:
                out.append(f"<tr><td>{esc(x.get('content'))}</td><td>{esc(x.get('interpreted'))}</td><td>{esc(x.get('page'))}</td></tr>")
            out.append("</table>")
        pay = m.get("payments") or {}
        if any(pay.values()):
            out.append("<h2>Payment</h2><div class='sub'>Bank: " + esc(pay.get("bank")) + " · Rek: "
                       + esc(pay.get("account")) + " a.n. " + esc(pay.get("account_name")) + "</div>")
        if m.get("missing"):
            out.append("<div class='sub'>missing: " + esc(", ".join(map(str, m["missing"]))) + "</div>")
        if m.get("notes"):
            out.append("<div class='sub'>notes: " + esc(m["notes"]) + "</div>")
    out.append("<h2>Raw standard JSON</h2><pre>" + H.escape(json.dumps(std, indent=2, default=str)) + "</pre>")
    return "".join(out)


@app.get("/", include_in_schema=False)
def root_redirect():
    return RedirectResponse("/view")


@app.get("/view", response_class=HTMLResponse)
def view_list(limit: int = 50):
    docs = list_documents(limit)["documents"]
    rows = "".join(
        f"<tr><td><a href='/view/{H.escape(d['id'])}'>{d['id'][:10]}…</a></td>"
        f"<td>{H.escape(d['filename'])}</td><td>{_badge(d['status'])}</td>"
        f"<td>{d['size']}</td><td>{d['created']}</td></tr>" for d in docs) or \
        "<tr><td colspan='5'>no documents yet</td></tr>"
    return _page("Doc Pipeline — Documents", f"""<h1>Doc Pipeline</h1>
<div class='sub'>Ingested documents, newest first · <a href='/view/inbox'>receiving inbox</a> · <a href='/view/settings'>OCR settings</a> · <a href='/docs'>API console</a></div>
<table><tr><th>ID</th><th>File</th><th>Status</th><th>Bytes</th><th>Created</th></tr>{rows}</table>""")


@app.get("/view/inbox", response_class=HTMLResponse)
def view_inbox():
    rs = list(reversed(_inbox))
    rows = "".join(
        f"<tr><td>{r['received_at']}</td>"
        f"<td><a href='/view/{H.escape(str(r['payload'].get('document_id','')))}'>{str(r['payload'].get('document_id',''))[:10]}…</a></td>"
        f"<td>{H.escape(str(r['payload'].get('filename','')))}</td>"
        f"<td><code>{str(r['payload'].get('task_id',''))[:8]}…</code></td></tr>" for r in rs) or \
        "<tr><td colspan='4'>empty since extractor start (memory only)</td></tr>"
    return _page("Receiving inbox", f"""<div class='nav'><a href='/view'>← documents</a></div>
<h1>Receiving API — inbox</h1>
<div class='sub'>Delivery payloads the mock receiving system actually got (in-memory, last 20)</div>
<table><tr><th>Received</th><th>Document</th><th>File</th><th>Task</th></tr>{rows}</table>""")


SETTINGS_HTML = """<!doctype html><html><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>OCR Settings — Doc Pipeline</title>
<style>body{font-family:system-ui,sans-serif;background:#f4f5f7;margin:0;padding:24px;color:#1a1a2e}
.card{max-width:640px;margin:auto;background:#fff;border-radius:12px;padding:24px;box-shadow:0 2px 8px #0001}
label{display:block;font-weight:600;margin:14px 0 4px}input,select{width:100%;padding:9px;border:1px solid #ccc;border-radius:8px;box-sizing:border-box;font-size:14px}
button{margin-top:16px;padding:10px 18px;border:0;border-radius:8px;background:#2563eb;color:#fff;font-size:14px;cursor:pointer}
button.sec{background:#e5e7eb;color:#111}#out{margin-top:14px;padding:10px;border-radius:8px;font-size:13px;white-space:pre-wrap;display:none}
.ok{background:#e7f7ec;color:#166534;display:block}.err{background:#fde8e8;color:#991b1b;display:block}.mut{background:#eef2ff;color:#3730a3;display:block}
.hint{font-size:12px;color:#666;margin-top:3px}.cur{font-size:12px;color:#555;background:#f1f5f9;padding:6px 8px;border-radius:6px;margin-top:4px}</style></head>
<body><div class='card'><h2>OCR Engine Settings</h2>
<div class='hint'>Saved to <code>data/ocr_config.json</code> — effective immediately, no restart. Env vars are bootstrap only.</div>
<div id='cur' class='cur'>loading…</div>
<label>Endpoint URL <span class='hint'>(OpenAI-compatible; base or full /chat/completions both OK)</span></label>
<input id='endpoint' placeholder='https://api.openai.com/v1'>
<label>API Key <span class='hint'>(blank = keep current)</span></label>
<input id='key' type='password' placeholder='sk-…' autocomplete='off'>
<label>Model</label>
<input id='model' list='models' placeholder='qwen3.8-flash'>
<datalist id='models'></datalist>
<button class='sec' onclick='fetchModels()'>Fetch models</button>
<label>Default engine <span class='hint'>(when caller omits engine)</span></label>
<select id='engine'><option value='qwen'>API vision (qwen-compatible)</option><option value='tesseract'>Tesseract (local, free)</option></select>
<label>Settings PIN <span class='hint'>(only if SETTINGS_PIN env is set)</span></label>
<input id='pin' type='password'>
<button onclick='testCfg()'>Test connection</button>
<button onclick='saveCfg()'>Save</button> <button class='sec' onclick='saveCfg(true)' title='skip the connection test'>Force save</button>
<pre id='out'></pre></div>
<script>
const $=id=>document.getElementById(id);
function say(cls,msg){const o=$('out');o.className=cls;o.textContent=msg;}
async function load(){
  const c=await(await fetch('/settings/ocr')).json();
  $('endpoint').value=c.endpoint||'';$('model').value=c.model||'';$('engine').value=c.engine||'qwen';
  $('cur').textContent='current: model='+(c.model||'—')+' · key='+(c.key_masked||'(none)')+' · engine='+c.engine+(c.source==='file'?' (file)':' (env bootstrap)');
}
function body(){return {endpoint:$('endpoint').value,key:$('key').value,model:$('model').value,engine:$('engine').value,pin:$('pin').value};}
async function fetchModels(){
  say('mut','listing models…');
  const r=await fetch('/settings/ocr/models',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body())});
  const d=await r.json();
  if(!r.ok)return say('err',d.detail||'failed');
  $('models').innerHTML=(d.models||[]).map(m=>`<option value='${m}'>`).join('');
  say('ok',(d.models||[]).length+' models — pick from the list');
}
async function testCfg(){
  say('mut','testing (tiny vision request)…');
  const r=await fetch('/settings/ocr/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body())});
  const d=await r.json();
  say(r.ok?'ok':'err',(r.ok?'✓ connection OK: ':'✗ ')+(d.result||d.detail||''));
}
async function saveCfg(force){
  if(!force){say('mut','testing before save…');
    const t=await fetch('/settings/ocr/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body())});
    if(!t.ok){const e=await t.json();return say('err','test failed, NOT saved: '+(e.detail||''));}}
  const b=body(); if(force)b.force=true;
  const r=await fetch('/settings/ocr',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});
  const d=await r.json();
  say(r.ok?'ok':'err',(r.ok?'✓ saved & live: '+JSON.stringify(d.config):'✗ '+(d.detail||'')));
  if(r.ok)load();
}
load();
</script></body></html>"""


@app.get("/view/settings", response_class=HTMLResponse)
def view_settings():
    return SETTINGS_HTML


def _pin_ok(body):
    pin = os.environ.get("SETTINGS_PIN", "")
    return (not pin) or body.get("pin") == pin


def _test_cfg(cfg):
    """Tiny black PNG, expect an 'ok'-ish reply. Translates 401/404/timeouts."""
    import urllib.error
    if not cfg.get("key"):
        raise HTTPException(400, "no API key (enter one, or leave the saved one in place)")
    if not cfg.get("endpoint"):
        raise HTTPException(400, "endpoint URL required")
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAKElEQVR4nGNgYGD4z4AGmNEF6IIRXQC3AgA1FwYBhyIPUQAAAABJRU5ErkJggg==")
    try:
        api_vision_ocr(png, {**cfg, "endpoint": normalize_chat_url(cfg["endpoint"])})
    except HTTPException:
        raise
    except urllib.error.HTTPError as e:
        hint = {401: "key rejected (401)", 403: "key forbidden (403)", 404: "model not available in this plan (404) — check the model name/plan", 429: "rate limited (429) — try again later"}.get(e.code, str(e.code))
        raise HTTPException(502, f"endpoint said {hint}")
    except Exception as e:
        raise HTTPException(502, f"cannot reach endpoint: {type(e).__name__}: {str(e)[:120]}")
    return "connection + model + key OK"


@app.get("/settings/ocr")
def get_settings():
    cfg = load_ocr_config()
    return {"endpoint": cfg["endpoint"], "model": cfg["model"], "engine": cfg["engine"],
            "key_masked": mask_key(cfg["key"]), "key_set": bool(cfg["key"]),
            "source": "file" if OCR_CONFIG_PATH.exists() else "env"}


@app.post("/settings/ocr")
async def post_settings(request: Request):
    body = await request.json()
    if not _pin_ok(body):
        raise HTTPException(401, "wrong settings PIN")
    key = (body.get("key") or "").strip() or load_ocr_config()["key"]
    cfg = {"endpoint": body.get("endpoint", ""), "model": (body.get("model") or "").strip(),
           "key": key, "engine": body.get("engine") or "qwen"}
    if not cfg["model"]:
        raise HTTPException(400, "model required")
    if not normalize_chat_url(cfg["endpoint"]):
        raise HTTPException(400, "endpoint URL required")
    # D4 gate: server-side test unless explicit force override
    if not body.get("force"):
        _test_cfg({**cfg, "endpoint": normalize_chat_url(cfg["endpoint"])})
    cfg = save_ocr_config(cfg)
    return {"ok": True, "tested": bool(not body.get("force")),
            "config": {"endpoint": cfg["endpoint"], "model": cfg["model"],
            "engine": cfg["engine"], "key_masked": mask_key(cfg["key"])}}


@app.post("/settings/ocr/test")
async def post_test(request: Request):
    body = await request.json()
    cur = load_ocr_config()
    cfg = {"endpoint": body.get("endpoint") or cur["endpoint"],
           "model": (body.get("model") or "").strip() or cur["model"],
           "key": (body.get("key") or "").strip() or cur["key"], "engine": "qwen"}
    return {"ok": True, "result": _test_cfg(cfg)}


@app.post("/settings/ocr/models")
async def post_models(request: Request):
    body = await request.json()
    cur = load_ocr_config()
    endpoint = normalize_chat_url(body.get("endpoint") or cur["endpoint"])
    listing = endpoint[: -len("/chat/completions")] + "/models" if endpoint.endswith("/chat/completions") else None
    if not listing:
        raise HTTPException(400, "endpoint must be OpenAI-compatible (/v1) to list models")
    key = (body.get("key") or "").strip() or cur["key"]
    import urllib.error
    req = urllib.request.Request(listing)
    req.add_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        raise HTTPException(502, f"{e.code} from {listing}")
    except Exception as e:
        raise HTTPException(502, f"cannot reach: {str(e)[:120]}")
    return {"models": sorted(m.get("id", "") for m in d.get("data", []) if m.get("id"))}


@app.get("/view/{doc_id}", response_class=HTMLResponse)
def view_document(doc_id: str):
    d = get_document(doc_id)
    std = d.get("standard_json") or {}
    payload = next((r["payload"] for r in reversed(_inbox)
                    if r["payload"].get("document_id") == doc_id), None)
    meta = [("Status", _badge(d["status"])), ("File", H.escape(d["filename"])),
            ("Doc type", H.escape(str(std.get("doc_type", "-")))),
            ("Notes", H.escape(str(std.get("notes") or "-"))),
            ("Size", f"{d['size']} bytes"), ("SHA-256", H.escape(d["sha256"])),
            ("Stored", H.escape(d["stored_path"])), ("Review loop", str(d.get("review_loop")))]
    kv = "".join(f"<div class='kv'><div class='k'>{k}</div><div class='v'>{v}</div></div>" for k, v in meta)
    jobs = "".join(f"<tr><td>{j['kind']}</td><td>{_badge(j['status'])}</td><td>{j['attempts']}</td><td>{H.escape(str(j['last_error'] or ''))}</td></tr>" for j in d["jobs"])
    tasks = "".join(f"<tr><td><code>{t['id'][:8]}…</code></td><td>{_badge(t['status'])}</td><td>{t['attempts']}</td><td>{H.escape(str(t['last_error'] or ''))}</td><td>{t['delivered_at'] or ''}</td><td>{H.escape(t['target_url'])}</td></tr>" for t in d["delivery_tasks"])
    payload_html = (("<h2>Delivered payload → receiving API</h2><pre>" + H.escape(json.dumps(payload, indent=2, default=str)) + "</pre>")
                    if payload else
                    "<h2>Delivered payload → receiving API</h2><div class='sub'>not in mock inbox (memory only — cleared on extractor restart, or delivered to a real target)</div>")
    eid = H.escape(doc_id)
    body = f"""<div class='nav'><a href='/view'>← all documents</a> · <a href='/documents/{eid}'>raw JSON</a> · <a href='/view/inbox'>inbox</a></div>
<h1>{H.escape(d['filename'])} &nbsp;{_badge(d['status'])}</h1>
<div class='sub'>created {d['created']} · updated {d['updated']}</div>
<div class='grid'>{kv}</div>
<h2>Extraction result (standard JSON)</h2>{_render_std(std)}
<h2>Jobs</h2><table><tr><th>Kind</th><th>Status</th><th>Attempts</th><th>Error</th></tr>{jobs}</table>
<h2>Delivery tasks</h2><table><tr><th>ID</th><th>Status</th><th>Att.</th><th>Last error</th><th>Delivered</th><th>Target</th></tr>{tasks}</table>
{payload_html}"""
    return _page(f"Doc · {d['filename']}", body)


# ---- mock receiving system ("other system" in the diagram) ----
_fail_next = {"force": False}
_inbox = []          # last received delivery payloads (proves what the receiving API got)


@app.post("/mock/receiving")
async def mock_receiving(request: Request):
    body = await request.json()
    if _fail_next["force"]:
        _fail_next["force"] = False
        raise HTTPException(503, "simulated downstream outage")
    receipt = {"accepted": True, "task_id": body.get("task_id"),
               "received_at": datetime.datetime.utcnow().isoformat() + "Z",
               "payload": body}
    _inbox.append(receipt)
    del _inbox[:-20]   # keep last 20
    return {"accepted": True, "task_id": body.get("task_id"),
            "received_at": receipt["received_at"]}


@app.get("/mock/receiving/inbox")
def mock_inbox(limit: int = 5):
    """What the receiving system actually got (newest first). Use this to assert
    the parsed-document JSON arrived intact."""
    return {"count": len(_inbox), "receipts": list(reversed(_inbox[-limit:]))}


@app.post("/mock/receiving/fail-next")
def mock_fail():
    _fail_next["force"] = True
    return {"ok": True, "note": "next delivery will 503"}


@app.get("/health")
def health():
    with db() as c:
        pass
    return {"ok": True, "db": True}
