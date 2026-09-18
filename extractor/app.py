"""Extractor service — implements: upload -> save original + create job ->
extract -> standard JSON -> validation.
Validation pass: publish to Service Bus (RabbitMQ exchange 'doc_pipeline',
routing key 'job.deliver') + commit delivery task row (transaction with job DONE).
Validation fail: flag fields + notify n8n review webhook (the loop back).
Also hosts /mock/receiving as stand-in downstream system."""
import os, io, json, base64, glob, hashlib, uuid, datetime, urllib.request
from pathlib import Path
import psycopg2, psycopg2.extras
from fastapi.responses import FileResponse
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
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
    return await ingest_one(raw, file.filename, file.content_type or "", doc_type, notes)


@app.post("/documents/batch")
async def upload_batch(request: Request):
    """Batch intake: {doc_type, notes, skip_existing=true, files:[{name,data_b64}]}
    Each file goes through the same single-document pipeline (own document/job/task);
    sha256 duplicates can be skipped -> SKIPPED_DUPLICATE."""
    body = await request.json()
    doc_type = body.get("doc_type") or "other"
    notes = body.get("notes") or ""
    skip = body.get("skip_existing", True)
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
QWEN_KEY = os.environ.get("QWEN_API_KEY", "")
QWEN_URL = os.environ.get("QWEN_URL",
    "https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1/chat/completions")
QWEN_MODEL = os.environ.get("QWEN_MODEL", "qwen3.8-flash")  # swap to qwen-vl-ocr later


def qwen_vision_ocr(png_bytes: bytes) -> str:
    import urllib.request as ur
    body = json.dumps({
        "model": QWEN_MODEL, "max_tokens": 1800,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(png_bytes).decode()}},
            {"type": "text", "text": "Extract ALL text from this document image exactly as written, preserving line structure. Include printed AND handwritten content. Output only the text."}]}]}).encode()
    req = ur.Request(QWEN_URL, data=body, method="POST")
    req.add_header("Authorization", "Bearer " + QWEN_KEY)
    req.add_header("Content-Type", "application/json")
    with ur.urlopen(req, timeout=180) as r:
        d = json.load(r)
    t = d["choices"][0]["message"]["content"]
    if isinstance(t, list):
        t = "\n".join(x.get("text", "") for x in t)
    return (t or "").strip()


def _tesseract(png_path: str, lang: str = "eng") -> str:
    import subprocess
    r = subprocess.run(["tesseract", png_path, "stdout", "-l", lang, "--psm", "3"],
                       capture_output=True, text=True, timeout=120)
    return r.stdout


@app.post("/documents/{doc_id}/ocr")
def run_ocr(doc_id: str, lang: str = "eng", engine: str = "tesseract"):
    """Option 1 (Tesseract): OCR the STORED original (pdf-scan or image),
    merge into standard_json, re-validate; on pass publish delivery like /correct."""
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
        if engine == "qwen" and not QWEN_KEY:
            raise HTTPException(500, "QWEN_API_KEY not configured on extractor")
        try:
            if mime == "application/pdf":
                import subprocess, tempfile
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
                                txt = qwen_vision_ocr(fh.read())
                        else:
                            txt = _tesseract(pg, lang)
                        pages_text.append({"page": i, "text": txt})
                std["kind"] = "pdf"
                std["ocr_engine"] = engine
            elif mime in ("image/jpeg", "image/png", "image/webp"):
                if engine == "qwen":
                    with open(path, "rb") as fh:
                        img_txt = qwen_vision_ocr(fh.read())
                else:
                    img_txt = _tesseract(path, lang)
                pages_text = [{"page": 1, "text": img_txt}]
                std["kind"] = "image"
                std["ocr_engine"] = engine
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
<div class='sub'>Ingested documents, newest first · <a href='/view/inbox'>receiving inbox</a> · <a href='/docs'>API console</a></div>
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
