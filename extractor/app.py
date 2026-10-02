"""Extractor service — implements: upload -> save original + create job ->
extract -> standard JSON -> validation.
Validation pass: publish to Service Bus (RabbitMQ exchange 'doc_pipeline',
routing key 'job.deliver') + commit delivery task row (transaction with job DONE).
Validation fail: flag fields + FLAGGED status (native review loop: auto-OCR chain
or human fix at /view/<id> -> revalidate -> deliver; n8n removed 2026-10-02).
Also hosts /mock/receiving as stand-in downstream system."""
import os, io, json, re, base64, glob, hashlib, uuid, datetime, urllib.request, urllib.parse, asyncio
import time
import threading
import subprocess, tempfile, shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
log = logging.getLogger("extractor")
from contextlib import contextmanager
from pathlib import Path
import psycopg2, psycopg2.extras
from pydantic import BaseModel
from fastapi.responses import FileResponse, HTMLResponse
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request, Response
import pika

UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "/data/uploads"))
INBOX_DIR = Path(os.environ.get("INBOX_DIR", "/data/inbox"))
DATABASE_URL = os.environ["DATABASE_URL"]
RABBIT_HOST = os.environ.get("RABBIT_HOST", "rabbitmq")
RABBIT_USER = os.environ.get("RABBIT_USER", "pipeline")
RABBIT_PASS = os.environ.get("RABBIT_PASS", "")
TARGET_API_URL = os.environ.get("TARGET_API_URL", "http://extractor:5000/mock/receiving")

app = FastAPI(title="doc-pipeline extractor")
psycopg2.extras.register_default_jsonb(loads=json.loads)


@contextmanager
def db():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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
        elif len(ext.get("pages", [])) > 1 and any(
                len((p.get("text") or "").strip()) < 20 and not (p.get("text") or "").strip().startswith("[BLANK_PAGE]")
                for p in ext.get("pages", [])):
            flags.append({"field": "extracted.pages.text", "value": "one-or-more-pages-near-empty",
                          "rule": "multi-page PDF has a page needing OCR before classification/splitting"})
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
                     doc_type: str, notes: str, folder: str = "", rel_path: str = "") -> dict:
    """Shared single-document pipeline: save original -> job -> extract -> validate
    -> publish (or flag). Used by /documents (multipart), /documents/batch and folder DFS.
    folder/rel_path = provenance from the DFS folder upload (empty for plain uploads)."""
    sha = hashlib.sha256(raw).hexdigest()
    doc_id = uuid.uuid4().hex
    mime = sniff_mime(raw, (declared_mime or "").split(";")[0], filename)
    suffix = MIME_SUFFIX.get(mime) or os.path.splitext(filename or "")[1].lstrip(".")[:5] or "bin"
    stored = UPLOAD_DIR / f"{sha[:16]}.{suffix}"
    stored.write_bytes(raw)
    with db() as c, c.cursor() as cur:
        cur.execute("INSERT INTO documents(id,filename,sha256,mime,size,stored_path,status,folder,rel_path) "
                    "VALUES(%s,%s,%s,%s,%s,%s,'UPLOADED',%s,%s)",
                    (doc_id, filename, sha, mime, len(raw), str(stored), folder or None, rel_path or None))
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
        elif result.get("status") == "VALIDATED":
            _map_enqueue([result["document_id"]])
            result["mapping_queued"] = 1
        if result.get("status") == "VALIDATED" and os.environ.get("MAP_AUTO", "0") == "1":
            _auto_map_bg([result["document_id"]])
            result["auto_map_started"] = 1
    return result




def _page_label(text: str):
    """Conservative page-header detector used before child-document mapping."""
    t = (text or "").lower()
    labels = [
        ("invoice", ("invoice", "faktur penjualan", "faktur pajak")),
        ("purchase_order", ("purchase order", "purchase order no", "po no")),
        ("sales_order", ("sales order", "so no")),
        ("delivery_order", ("delivery order", "delivery note", "surat jalan")),
        ("tanda_terima", ("tanda terima", "goods received", "diterima oleh")),
        ("bukti_transfer", ("bukti transfer", "transfer berhasil", "transaction reference")),
        ("kwitansi", ("kwitansi", "receipt no")),
        ("work_order", ("work order", "work order no")),
        ("kontrak", ("perjanjian", "kontrak", "agreement")),
    ]
    hits = [(label, min(t.find(k) for k in keys if k in t))
            for label, keys in labels if any(k in t for k in keys)]
    return min(hits, key=lambda x: x[1])[0] if hits else None


def _detect_pdf_parts(std: dict):
    """Return conservative page ranges only when distinct document headers occur.
    Multi-page documents of one type remain one part; uncertain boundaries are not split."""
    pages = std.get("pages") or []
    markers = [(p.get("page", i + 1), _page_label(p.get("text", "")))
               for i, p in enumerate(pages)]
    markers = [(p, k) for p, k in markers if k]
    if len({k for _, k in markers}) < 2:
        return []
    starts = []
    seen = set()
    for page, label in markers:
        if label not in seen:
            starts.append((page, label)); seen.add(label)
    if len(starts) < 2:
        return []
    ranges = []
    for i, (start, label) in enumerate(starts):
        end = starts[i + 1][0] - 1 if i + 1 < len(starts) else len(pages)
        if end >= start:
            ranges.append({"page_start": start, "page_end": end, "detected_class": label,
                           "confidence": "medium"})
    return ranges if len(ranges) > 1 else []


def _create_child_pdf(parent: dict, raw: bytes, part: dict):
    """Create a child document without publishing it before child mapping."""
    from pypdf import PdfReader, PdfWriter
    reader = PdfReader(io.BytesIO(raw)); writer = PdfWriter()
    for idx in range(part["page_start"] - 1, part["page_end"]):
        writer.add_page(reader.pages[idx])
    out = io.BytesIO(); writer.write(out); child_raw = out.getvalue()
    sha = hashlib.sha256(child_raw).hexdigest()
    child_id = uuid.uuid4().hex
    filename = f"{Path(parent['filename']).stem}_p{part['page_start']}-{part['page_end']}.pdf"
    stored = UPLOAD_DIR / f"{sha[:16]}.pdf"; stored.write_bytes(child_raw)
    mime = "application/pdf"
    std = extract_pdf(child_raw)
    std.update({"schema_version": "1.0", "document_id": child_id, "filename": filename,
                "sha256": sha, "uploaded_at": datetime.datetime.utcnow().isoformat() + "Z",
                "doc_type": "other", "notes": "child document split from parent",
                "parent_document_id": parent["id"], "page_start": part["page_start"],
                "page_end": part["page_end"], "is_child": True})
    ok, flags = validate({"schema_version": "1.0", "document_id": child_id,
                          "filename": filename, "sha256": sha, "extracted": std})
    status = "VALIDATED" if ok else "FLAGGED"
    with db() as c, c.cursor() as cur:
        cur.execute("INSERT INTO documents(id,filename,sha256,mime,size,stored_path,status,standard_json,validation,parent_document_id,page_start,page_end) VALUES(%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s)",
                    (child_id, filename, sha, mime, len(child_raw), str(stored), status,
                     json.dumps(std), json.dumps({"passed": ok, "flagged_fields": flags}),
                     parent["id"], part["page_start"], part["page_end"]))
        cur.execute("INSERT INTO document_parts(parent_document_id,child_document_id,page_start,page_end,detected_class,confidence) VALUES(%s,%s,%s,%s,%s,%s)",
                    (parent["id"], child_id, part["page_start"], part["page_end"],
                     part["detected_class"], part["confidence"]))
        c.commit()
    return child_id


def _split_parent_if_needed(did: str, std: dict, filename: str):
    if std.get("is_child") or std.get("parts_created") or std.get("kind") != "pdf":
        return []
    with db() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT id,filename,stored_path FROM documents WHERE id=%s", (did,)); parent = cur.fetchone()
    if not parent or not os.path.exists(parent["stored_path"]):
        return []
    parts = _detect_pdf_parts(std)
    if len(parts) < 2:
        return []
    raw = Path(parent["stored_path"]).read_bytes()
    children = [_create_child_pdf(parent, raw, p) for p in parts]
    std["parts_created"] = True
    std["document_scope"] = "multiple"
    std["parts"] = [{**p, "child_document_id": cid} for p, cid in zip(parts, children)]
    with db() as c, c.cursor() as cur:
        cur.execute("UPDATE documents SET standard_json=%s::jsonb, updated=now() WHERE id=%s", (json.dumps(std), did)); c.commit()
    print("SPLIT", did[:8], "->", len(children), "children", flush=True)
    return children


def _map_one_now(did: str):
    with db() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT filename,standard_json FROM documents WHERE id=%s", (did,)); row = cur.fetchone()
    if not row: return
    std = dict(row["standard_json"] or {})
    if (std.get("mapped") and not isinstance(std.get("mapped"), dict)) or std.get("classified"): return
    classify_and_route(did, std, row["filename"], explicit_invoice=False)
    task_id = None
    with db() as c, c.cursor() as cur:
        cur.execute("UPDATE documents SET standard_json=%s::jsonb, status='VALIDATED', updated=now() WHERE id=%s", (json.dumps(std), did))
        if std.get("mapped") or std.get("classified"):
            task_id = str(uuid.uuid4())
            cur.execute("INSERT INTO delivery_tasks(id,document_id,target_url,status) VALUES(%s,%s,%s,'QUEUED')",
                        (task_id, did, TARGET_API_URL))
        c.commit()
    if std.get("mapped") or std.get("classified"):
        publish("deliver", {"task_id": task_id, "document_id": did})
    if std.get("mapped"):
        upsert_invoice_row(did, row["filename"], std)
    print("AUTO-MAP(child)", did[:8], "->", (std.get("classified") or {}).get("class"),
          "delivery=" + (task_id or "none"), flush=True)


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
                if (std.get("mapped") and not isinstance(std.get("mapped"), dict)) or std.get("classified") or std.get("parts_created"):
                    continue
                children = _split_parent_if_needed(did, std, row["filename"])
                if children:
                    valid_children, flagged_children = [], []
                    with db() as c, c.cursor() as cur:
                        for child_id in children:
                            cur.execute("SELECT status FROM documents WHERE id=%s", (child_id,))
                            (flagged_children if (cur.fetchone() or [""])[0] == "FLAGGED" else valid_children).append(child_id)
                    for child_id in valid_children: _map_one_now(child_id)
                    if flagged_children: _auto_ocr_bg(flagged_children)
                    continue
                classify_and_route(did, std, row["filename"],
                                   explicit_invoice=(std.get("doc_type") or "").lower() == "invoice")
                mapping_error = isinstance(std.get("mapped"), dict) and bool(std["mapped"].get("error"))
                env = {"schema_version": std.get("schema_version", "1.0"), "document_id": did,
                       "filename": row["filename"], "extracted": std}
                ok, flags = validate(env)
                if mapping_error:
                    ok = False
                    flags = list(flags) + [{"rule": "mapping completed", "field": "mapped", "value": "error"}]
                task_id = None
                final_status = "VALIDATED" if ok else "FLAGGED"
                with db() as c, c.cursor() as cur:
                    cur.execute("UPDATE documents SET standard_json=%s::jsonb, validation=%s::jsonb, status=%s, updated=now() WHERE id=%s",
                                (json.dumps(std), json.dumps({"passed": ok, "flagged_fields": flags}), final_status, did))
                    if ok and not std.get("parts_created"):
                        task_id = str(uuid.uuid4())
                        cur.execute("INSERT INTO delivery_tasks(id,document_id,target_url,status) VALUES(%s,%s,%s,'QUEUED')",
                                    (task_id, did, TARGET_API_URL))
                    c.commit()
                if task_id:
                    publish("deliver", {"task_id": task_id, "document_id": did})
                if std.get("mapped"):
                    try:
                        upsert_invoice_row(did, row["filename"], std)
                    except Exception as ue:
                        print("LEDGER upsert (digital) failed:", str(ue)[:150], flush=True)
                print("AUTO-MAP(digital)", did[:8], "->",
                      (std.get("classified") or {}).get("class"),
                      "mapped" if std.get("mapped") else "profile",
                      "delivery=" + (task_id or "none"), flush=True)
            except Exception as e:
                print("AUTO-MAP(digital)", did[:8], "FAILED:", str(e)[:200], flush=True)
    threading.Thread(target=worker, daemon=True).start()


def _auto_ocr_bg(doc_ids):
    """Option: auto-OCR (default engine) for flagged docs, sequential background thread.
    Triggered right after /documents/batch when OCR_AUTO=1."""
    print("AUTO-OCR-QUEUED", ",".join(str(x)[:8] for x in doc_ids), flush=True)
    def one(did):
        r = run_ocr(did)
        print("AUTO-OCR", did[:8], "->", r.get("status"), r.get("chars"), "chars", flush=True)
        if r.get("status") == "VALIDATED":
            _map_enqueue([did])

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


def _recover_pending_bg():
    """Recover interrupted OCR/mapping after service restart; DB is the queue of record."""
    def worker():
        time.sleep(3)
        try:
            with db() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""SELECT id, status, standard_json FROM documents
                              WHERE status='FLAGGED' AND COALESCE(standard_json->>'ocr_engine','')=''
                              ORDER BY created LIMIT 50""")
                flagged = [r["id"] for r in cur.fetchall()]
                cur.execute("""SELECT id FROM documents
                              WHERE status='FLAGGED' AND standard_json IS NOT NULL
                                AND standard_json ? 'ocr_engine'
                              ORDER BY created LIMIT 50""")
                map_pending = [r["id"] for r in cur.fetchall()]
                cur.execute("""SELECT id FROM documents
                              WHERE status='VALIDATED' AND standard_json IS NOT NULL
                                AND NOT (standard_json ? 'classified')
                              ORDER BY created LIMIT 50""")
                validated = [r["id"] for r in cur.fetchall()]
            if os.environ.get("OCR_AUTO", "1") == "1" and flagged:
                print("AUTO-RECOVER-OCR", len(flagged), flush=True)
                _auto_ocr_bg(flagged)
            if os.environ.get("MAP_AUTO", "0") == "1" and map_pending:
                print("AUTO-RECOVER-MAP-PENDING", len(map_pending), flush=True)
                _auto_map_bg(map_pending)
            if os.environ.get("MAP_AUTO", "0") == "1" and validated:
                print("AUTO-RECOVER-MAP", len(validated), flush=True)
                _auto_map_bg(validated)
        except Exception as e:
            print("AUTO-RECOVER FAILED:", str(e)[:300], flush=True)
    threading.Thread(target=worker, daemon=True).start()


@app.on_event("startup")
def recover_pending_on_startup():
    _recover_pending_bg()


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
    if len(files) > 10000:
        raise HTTPException(400, "batch max 10000 files")
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
    if os.environ.get("OCR_AUTO", "1") == "1":
        flagged = [r["document_id"] for r in results if r.get("status") == "FLAGGED"]
        validated = [r["document_id"] for r in results if r.get("status") == "VALIDATED"]
        if flagged:
            _auto_ocr_bg(flagged)
        if validated:
            _map_enqueue(validated)
        if validated and os.environ.get("MAP_AUTO", "0") == "1":
            _auto_map_bg(validated)
        return {"batch": True, "total": len(results), "summary": counts,
                "auto_ocr_started": len(flagged), "auto_map_started": len(validated), "results": results}
    return {"batch": True, "total": len(results), "summary": counts, "results": results}


# ---------------------------------------------------------------------------
# Folder / DFS ingestion (2026-10-02): walk a directory tree, find PDFs
# (and images/xlsx), ingest every file in parallel workers, auto-OCR each.
# ---------------------------------------------------------------------------

def _walk_find_docs(root: Path, exts=None):
    """DFS (depth-first) over a directory tree; yield files whose extension is
    a supported document type (pdf/jpg/jpeg/png/webp/xlsx/txt by default).
    Hidden entries and symlink cycles are skipped."""
    exts = exts or set(MIME_SUFFIX.values())
    seen = set()
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name)
        except OSError:
            continue
        dirs, files = [], []
        for e in entries:
            if e.name.startswith('.'):
                continue
            try:
                if e.is_dir(follow_symlinks=False):
                    rp = os.path.realpath(e.path)
                    if rp in seen:
                        continue
                    seen.add(rp)
                    dirs.append(Path(e.path))
                elif e.is_file(follow_symlinks=True):
                    files.append(e)
            except OSError:
                continue
        for e in files:
            f = Path(e.path)
            if f.suffix.lower().lstrip('.') in exts:
                yield f
        stack.extend(reversed(dirs))   # DFS: descend subdirs depth-first


async def _process_one_file(f: Path, folder_name: str, doc_type: str,
                            skip_existing: bool, allow_dup: bool) -> dict:
    try:
        raw = f.read_bytes()
    except OSError as e:
        return {"rel_path": str(f), "status": "ERROR", "error": f"read failed: {e}"[:200]}
    return await _process_bytes(raw, f.name, folder_name, doc_type, skip_existing, allow_dup,
                                 rel=str(f.relative_to(Path(folder_name))) if folder_name else f.name)


async def _process_bytes(raw: bytes, name: str, folder_name: str, doc_type: str,
                         skip_existing: bool, allow_dup: bool, rel: str = "") -> dict:
    if not raw:
        return {"rel_path": rel or name, "status": "ERROR", "error": "empty file"}
    sha = hashlib.sha256(raw).hexdigest()
    if skip_existing:
        dup = find_by_sha(sha)
        if dup:
            return {"rel_path": rel or name, "sha256": sha, "status": "SKIPPED_DUPLICATE",
                    "document_id": dup["document_id"], "existing_status": dup["status"]}
    try:
        r = await ingest_one(raw, name, "", doc_type,
                             f"folder upload: {folder_name}",
                             folder=folder_name or "", rel_path=rel or "")
    except HTTPException as e:
        r = {"sha256": sha, "status": "EXTRACT_FAILED", "error": str(e.detail)[:200]}
    r["rel_path"] = rel or name
    return r


def _folder_job_state():
    st = getattr(_folder_job_state, "_st", None)
    if st is None:
        st = _folder_job_state._st = {"running": False, "folder": "", "total": 0, "processed": 0,
                                      "counts": {}, "started": "", "finished": "", "error": "",
                                      "last_files": [], "log": []}
    return st


def _folder_ingest_bg(files, folder_name: str, doc_type: str, skip_existing: bool,
                      allow_dup: bool, source_label: str, spool: Path = None):
    """Background: ingest every file with N workers, OCR each right after intake
    (same worker — intake+OCR of one doc is sequential, docs run in parallel).
    files: list of ("path", Path, rel) or ("bytes", name, raw, rel).
    OCR_WORKERS env (live: 4) controls parallelism; OCR_AUTO=0 keeps intake only."""
    st = _folder_job_state()
    if st["running"]:
        return False
    st.update({"running": True, "folder": source_label, "total": len(files), "processed": 0,
               "counts": {}, "started": datetime.datetime.utcnow().isoformat() + "Z",
               "finished": "", "error": "", "last_files": [], "log": []})

    nworkers = max(1, int(os.environ.get("OCR_WORKERS", "4")))
    auto_ocr = os.environ.get("OCR_AUTO", "1") == "1"
    results = []
    rlock = threading.Lock()

    def work(entry):
        if entry[0] == "path":
            _, fpath, rel = entry
            name = Path(fpath).name
            try:
                raw = Path(fpath).read_bytes()
            except OSError as e:
                return {"rel_path": rel, "status": "ERROR", "error": f"read failed: {str(e)[:180]}"}
        else:
            _, name, raw, rel = entry
        r = asyncio.run(_process_bytes(raw, name, folder_name, doc_type, skip_existing, allow_dup, rel=rel))
        if auto_ocr and r.get("document_id") and r.get("status") == "FLAGGED":
            try:
                o = run_ocr(r["document_id"])
                r["ocr"] = {"status": o.get("status"), "chars": o.get("chars")}
            except Exception as e:
                r["ocr"] = {"status": "OCR_FAILED", "error": str(e)[:180]}
        elif auto_ocr and r.get("document_id") and r.get("status") == "VALIDATED":
            r["ocr"] = {"status": "SKIPPED_DIGITAL"}
        if r.get("document_id") and (r.get("status") == "VALIDATED"
                                     or (r.get("ocr") or {}).get("status") == "VALIDATED"):
            _map_enqueue([r["document_id"]])
        return r

    def worker():
        try:
            with ThreadPoolExecutor(max_workers=nworkers) as pool:
                for r in pool.map(work, files):
                    with rlock:
                        results.append(r)
                        st["processed"] = len(results)
                        c = {}
                        for x in results:
                            c[x["status"]] = c.get(x["status"], 0) + 1
                        st["counts"] = c
                        st["last_files"] = [x.get("rel_path", "")[:60] for x in results[-3:]]
                        st["log"].append(f"{st['processed']}/{st['total']} {r.get('rel_path','')} -> {r['status']}")
                        st["log"] = st["log"][-50:]
        except Exception as e:
            st["error"] = str(e)[:300]
        finally:
            st["running"] = False
            st["finished"] = datetime.datetime.utcnow().isoformat() + "Z"
            if spool:
                try:
                    shutil.rmtree(spool, ignore_errors=True)
                except Exception:
                    pass
            print(f"FOLDER-INGEST DONE {source_label}: {st['counts']}", flush=True)

    threading.Thread(target=worker, daemon=True).start()
    return True


@app.post("/documents/folder")
async def upload_folder(request: Request,
                        files: list[UploadFile] = File(...),
                        doc_type: str = Form(""),
                        folder_name: str = Form(""),
                        notes: str = Form("")):
    """DFS folder upload (multipart): browser sends every file in the picked folder
    (webkitdirectory) with its relative path in the filename field. Files spool to
    disk (no RAM blowup on big trees), then ingest + auto-OCR run in background workers."""
    doc_type, _ = _meta_from_query(request, doc_type, notes)
    allow_dup = os.environ.get("INGEST_ALLOW_DUP", "0") == "1"
    job = uuid.uuid4().hex[:12]
    spool = Path(os.environ.get("SPOOL_DIR", "/data/spool")) / job
    spool.mkdir(parents=True, exist_ok=True)
    entries = []
    for f in files:
        rp = (f.filename or "unnamed").replace("\\", "/").lstrip("/")
        parts = [p for p in rp.split("/") if p not in ("", ".", "..")]
        name = parts[-1] if parts else "unnamed"
        rel = "/".join(parts)
        dest = spool / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as out:
            while True:
                chunk = await f.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
        entries.append(("path", dest, rel))
    if not entries:
        shutil.rmtree(spool, ignore_errors=True)
        raise HTTPException(400, "no files")
    started = _folder_ingest_bg(entries, folder_name or "", doc_type, True, allow_dup,
                                f"upload:{folder_name or 'folder'} ({len(entries)} files)",
                                spool=spool.parent)
    if not started:
        shutil.rmtree(spool, ignore_errors=True)
        raise HTTPException(409, "a folder ingestion is already running — check GET /documents/folder/status")
    return {"accepted": True, "total": len(entries), "status_url": "/documents/folder/status"}


@app.post("/documents/folder/scan")
async def scan_folder(body: dict):
    """Server-side DFS: path is a directory mounted inside the extractor container
    (e.g. /data/inbox from the compose volume). Walks it depth-first, finds PDFs +
    images + xlsx, ingests + auto-OCR every document found, in background workers."""
    p = Path((body or {}).get("path") or "")
    if not str(p).startswith("/") or any(x == ".." for x in p.parts):
        raise HTTPException(400, "absolute path inside the container required (no ..); mount folders under /data")
    if not p.is_dir():
        raise HTTPException(404, f"not a directory: {p}")
    allow_dup = os.environ.get("INGEST_ALLOW_DUP", "0") == "1"
    files = list(_walk_find_docs(p))
    if not files:
        return {"accepted": False, "total": 0, "note": "no supported documents found", "root": str(p)}
    entries = [("path", f, str(f.relative_to(p))) for f in files]
    started = _folder_ingest_bg(entries, str(p), body.get("doc_type") or "other",
                                bool(body.get("skip_existing", True)), allow_dup,
                                f"scan:{p}")
    if not started:
        raise HTTPException(409, "a folder ingestion is already running — check GET /documents/folder/status")
    return {"accepted": True, "total": len(files), "root": str(p),
            "files": [e[2] for e in entries][:200],
            "status_url": "/documents/folder/status"}


@app.get("/documents/folder/status")
def folder_status():
    return _folder_job_state()


# ---------------- mapping queue & monitor (flow: upload -> OCR -> map -> monitor) ----
# DB is the queue of record (map_tasks, db-init/09). The mapping ENGINE is a client:
#   'hermes' = deterministic parsers in scripts/ driving the HTTP API
#   'api'    = LLM token-plan call fed with the SAME rules digest (knowledge/)
# Server enforces the hard rules on every rows POST (gate_rows); engine cannot sneak values.
KNOWLEDGE_DIR = Path(os.environ.get("KNOWLEDGE_DIR", "/knowledge"))

# sheet-exact headers + the DB columns behind each (order = sheet column order)
MAP_SHEET_HEADERS = {
    "faktur_pajak": ["SOR", "Billing Number", "Kode Seri", "NPWP & NITKU Pengusaha",
        "Dasar Pengenaan Pajak", "PPN", "Tanggal Transaksi", "NPWP & NITKU Pembeli",
        "Nama Pembeli", "Nama Barang Kena Pajak", "Qty", "Harga Satuan", "Jumlah Harga",
        "Potongan Harga", "Uang Muka", "PPN Dev", "PPnBM", "Harga Jual Total", "Source File"],
    "faktur_penjualan": ["Kode Material", "SOR", "Kemasan", "Nama Produk", "Qty", "Harga",
        "Disc 1", "Disc 2", "Disc 3", "Disc 4", "Disc 5", "Jumlah", "Dasar Pengenaan Pajak",
        "PPN", "Total", "Source Page", "Confidence", "Review Status"],
    "po_customer": ["Purchase Order No", "Vendor Code (SAMB @ client)", "PO Issuer (Customer)",
        "PPN", "Product Code", "Product Name", "Qty", "UON", "Unit Price", "Discount",
        "Total", "Source Page", "Mapping Status"],
    "tanda_terima": ["Posting Date", "Document No", "Purchase Order No", "Vendor Number",
        "Item Code", "Material Description", "Qty", "UON", "Source Page", "Mapping Status"],
}
MAP_EXPORT_COLS = {
    "faktur_pajak": ["sor", "billing_number", "kode_seri", "npwp_pengusaha",
        "dasar_pengenaan_pajak", "ppn", "tanggal_transaksi", "npwp_pembeli", "nama_pembeli",
        "nama_bkp", "qty", "harga_satuan", "jumlah_harga", "potongan_harga", "uang_muka",
        "ppn_dev", "ppnbm", "harga_jual_total", "coalesce(rel_path, source_file)"],
    "faktur_penjualan": ["kode_material", "sor", "kemasan", "nama_produk", "qty", "harga",
        "disc_1", "disc_2", "disc_3", "disc_4", "disc_5", "jumlah", "dasar_pengenaan_pajak",
        "ppn", "total", "source_page", "confidence", "coalesce(review_status, mapping_status)"],
    "po_customer": ["purchase_order_no", "vendor_code", "po_issuer", "ppn", "product_code",
        "product_name", "qty", "uon", "unit_price", "discount", "total", "source_page",
        "mapping_status"],
    "tanda_terima": ["posting_date", "document_no", "purchase_order_no", "vendor_number",
        "item_code", "material_description", "qty", "uon", "source_page", "mapping_status"],
}
MAP_LABEL = {"faktur_pajak": "Faktur Pajak", "faktur_penjualan": "Faktur Penjualan",
             "po_customer": "PO Customer", "tanda_terima": "Tanda Terima"}


MAP_COLS = {
    "faktur_pajak": ["sor", "billing_number", "kode_seri", "npwp_pengusaha",
                     "dasar_pengenaan_pajak", "ppn", "tanggal_transaksi", "npwp_pembeli",
                     "nama_pembeli", "nama_bkp", "qty", "harga_satuan", "jumlah_harga",
                     "potongan_harga", "uang_muka", "ppn_dev", "ppnbm", "harga_jual_total",
                     "source_file", "confidence", "review_status", "mapping_status"],
    "faktur_penjualan": ["kode_material", "sor", "kemasan", "nama_produk", "qty", "harga",
                         "disc_1", "disc_2", "disc_3", "disc_4", "disc_5", "jumlah",
                         "dasar_pengenaan_pajak", "ppn", "total", "source_page",
                         "confidence", "review_status", "mapping_status"],
    "po_customer": ["purchase_order_no", "vendor_code", "po_issuer", "ppn", "product_code",
                    "product_name", "qty", "uon", "unit_price", "discount", "total",
                    "source_page", "mapping_status"],
    "tanda_terima": ["posting_date", "document_no", "purchase_order_no", "vendor_number",
                     "item_code", "material_description", "qty", "uon", "source_page",
                     "mapping_status"],
}
MAP_NUM = {t: {c for c in cols if c in (
    "dasar_pengenaan_pajak", "ppn", "qty", "harga_satuan", "jumlah_harga", "potongan_harga",
    "uang_muka", "ppn_dev", "ppnbm", "harga_jual_total", "harga", "disc_1", "disc_2",
    "disc_3", "disc_4", "disc_5", "jumlah", "total", "unit_price", "discount")}
    for t, cols in MAP_COLS.items()}
MAP_DATE = {"faktur_pajak": {"tanggal_transaksi"}, "tanda_terima": {"posting_date"}}
MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MEI": 5, "MAY": 5, "JUN": 6, "JUL": 7,
          "AGU": 8, "AUG": 8, "SEP": 9, "OCT": 10, "OKT": 10, "NOV": 11, "DES": 12, "DEC": 12}
MONTHS_FULL = {1: "januari", 2: "februari", 3: "maret", 4: "april", 5: "mei", 6: "juni",
               7: "juli", 8: "agustus", 9: "september", 10: "oktober", 11: "november", 12: "desember"}


def _norm_cell(tab, col, v):
    """Coerce one incoming cell to its column type. Never invents: junk -> None."""
    if v is None or v == "":
        return None
    if col in MAP_NUM.get(tab, ()):
        s = str(v).strip().split("/")[0].strip()
        if s and re.fullmatch(r"[-\d.,]+", s):
            if "," in s and ("." in s or re.search(r",\d{1,2}$", s)):
                s2 = s.replace(".", "").replace(",", ".")
            else:
                s2 = s.replace(",", "")
            try:
                f = float(s2)
            except ValueError:
                return None
            return f if abs(f) < 1e12 else None      # OCR money-misread guard (no-guess)
        return None
    if col in MAP_DATE.get(tab, set()):
        s = str(v).strip().lower()
        m = re.match(r"(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})", s)
        if m:
            d, mo, y = (int(m.group(i)) for i in (1, 2, 3)); y += 2000 if y < 100 else 0
            try: return datetime.date(y, mo, d).isoformat()
            except ValueError: return None
        m = re.match(r"(\d{1,2})[/-]([a-z]{3})[/-](\d{2,4})", s)
        if m and m.group(2).upper() in MONTHS:
            y = int(m.group(3)); y += 2000 if y < 100 else 0
            try: return datetime.date(y, MONTHS[m.group(2).upper()], int(m.group(1))).isoformat()
            except ValueError: return None
        m = re.match(r"(\d{1,2})\s+([a-z]+)\s+(\d{4})", s)
        if m:
            mon = {v3: k for k, v3 in MONTHS_FULL.items()}.get(m.group(2))
            if mon:
                try: return datetime.date(int(m.group(3)), mon, int(m.group(1))).isoformat()
                except ValueError: return None
        return None
    if col == "source_page":
        m = re.search(r"p?(\d+)", str(v))
        return int(m.group(1)) if m else None
    return str(v).strip()[:280] or None


def gate_rows(tab, rows):
    """Hard rules (knowledge/po_rules.json, user policy) enforced SERVER-side.
    Returns list of rejection strings; empty = accept. Arithmetic mismatch does NOT
    reject but DEMOTES MAPPED -> REVIEW-ARITH (honest status beats invented numbers)."""
    errs = []
    for i, r in enumerate(rows):
        if tab == "po_customer":
            if not str(r.get("product_name") or "").strip():
                errs.append(f"row{i}: product_name required (PO rule)"); continue
            bad = next((f for f in ("po_issuer", "vendor_code")
                        if "sarana abadi" in str(r.get(f) or "").lower()), None)
            if bad:
                errs.append(f"row{i}: SAMB never a {bad}"); continue
            ppn = r.get("ppn")
            if ppn not in (None, "", "11%", "1.1%"):
                errs.append(f"row{i}: PPN must be 11%/1.1% (got {ppn})"); continue
            q, p, d, t = (r.get(c) for c in ("qty", "unit_price", "discount", "total"))
            if None not in (q, p, t) and str(r.get("mapping_status") or "").startswith("MAPPED"):
                net = q * p - (d or 0.0)
                ok = abs(net - t) <= max(1.0, abs(t) * 0.02)
                if not ok and ppn:
                    f = 1.11 if ppn == "11%" else 1.011
                    ok = abs(net * f - t) <= max(1.0, abs(t) * 0.02)
                if not ok:
                    r["mapping_status"] = "REVIEW-ARITH|" + str(r.get("mapping_status") or "")
        if tab == "faktur_pajak":
            v, dpp = r.get("ppn"), r.get("dasar_pengenaan_pajak")
            if v is not None and dpp:
                if not any(abs(dpp * f - v) <= max(500.0, abs(v) * 0.01) for f in (0.11, 0.011)):
                    r["review_status"] = ((r.get("review_status") or "") + ";PPN-DPP-MISMATCH").strip(";")
    return errs


def _tab_guess(folder, rel_path, filename):
    fp = urllib.parse.unquote(str(rel_path or filename or "")).lower()
    if "faktur pajak" in fp: return "faktur_pajak"
    if "faktur penjualan" in fp or re.search(r"(^|/)1 f", fp): return "faktur_penjualan"
    if re.search(r"purchase order|(^|/)2 po", fp): return "po_customer"
    if re.search(r"ttg|receiving|terima barang|bukti penerimaan|supply sheet|good receipt|bpb|penerimaan barang", fp):
        return "tanda_terima"
    return "other"


def _map_enqueue(doc_ids):
    """Queue docs for mapping (idempotent; done tasks are NOT resurrected)."""
    n = 0
    try:
        with db() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            for did in doc_ids:
                cur.execute("SELECT folder, rel_path, filename FROM documents WHERE id=%s", (did,))
                r = cur.fetchone()
                if not r: continue
                g = _tab_guess(r["folder"], r["rel_path"], r["filename"])
                cur.execute(
                    """INSERT INTO map_tasks(document_id, folder, rel_path, tab_guess)
                       VALUES(%s,%s,%s,%s)
                       ON CONFLICT (document_id) DO UPDATE
                         SET tab_guess=EXCLUDED.tab_guess, folder=EXCLUDED.folder,
                             rel_path=EXCLUDED.rel_path, updated_at=now()
                       WHERE map_tasks.status IN ('pending','failed','claimed')""",
                    (did, r["folder"], r["rel_path"], g))
                if cur.rowcount: n += 1
    except Exception as e:
        print("MAP-ENQUEUE failed:", str(e)[:200], flush=True)
    if n: print("MAP-ENQUEUE", n, flush=True)
    return n


@app.post("/mapping/enqueue")
async def mapping_enqueue(request: Request):
    """Manual/backfill enqueue: {"document_ids":[...]} or {"folder":"Complete bundles%"} or {"all":true}."""
    body = {}
    try: body = await request.json()
    except Exception: pass
    ids = []
    with db() as c, c.cursor() as cur:
        if body.get("document_ids"):
            ids = list(body["document_ids"])
        elif body.get("folder"):
            cur.execute("SELECT id FROM documents WHERE folder LIKE %s AND status IN ('VALIDATED','DELIVERED')",
                        (body["folder"],)); ids = [r[0] for r in cur.fetchall()]
        elif body.get("all"):
            cur.execute("SELECT id FROM documents WHERE status IN ('VALIDATED','DELIVERED')"); ids = [r[0] for r in cur.fetchall()]
    return {"requested": len(ids), "enqueued": _map_enqueue(ids)}


@app.get("/mapping/pending")
def mapping_pending(limit: int = 50, folder: str = ""):
    with db() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        q = """SELECT t.document_id, t.folder, t.rel_path, t.tab_guess, t.status, t.attempts, t.error,
                      d.filename, d.status AS doc_status
               FROM map_tasks t JOIN documents d ON d.id = t.document_id
               WHERE t.status IN ('pending','failed')
                 AND d.status IN ('VALIDATED','DELIVERED')"""
        args = []
        if folder: q += " AND t.folder LIKE %s"; args.append(folder)
        q += " ORDER BY t.created_at LIMIT %s"; args.append(min(int(limit), 500))
        cur.execute(q, args)
        tasks = [dict(r) for r in cur.fetchall()]
    return {"count": len(tasks), "tasks": tasks}


@app.post("/mapping/claim")
async def mapping_claim(request: Request):
    body = await request.json()
    ids = list(body.get("document_ids") or [])
    engine = str(body.get("engine") or "hermes")[:20]
    with db() as c, c.cursor() as cur:
        cur.execute("""UPDATE map_tasks SET status='claimed', engine=%s, claimed_at=now(),
                              attempts=attempts+1, updated_at=now()
                       WHERE document_id = ANY(%s) AND status IN ('pending','failed')
                       RETURNING document_id""", (engine, ids))
        got = [r[0] for r in cur.fetchall()]
    return {"claimed": got}


@app.post("/documents/{doc_id}/mapping/rows")
async def mapping_rows(doc_id: str, request: Request):
    """Engine submits mapped rows for ONE target tab. Server: normalizes types,
    enforces rules (gate_rows), replaces this doc's rows for this tab, marks task mapped."""
    body = await request.json()
    tab = body.get("tab")
    rows = list(body.get("rows") or [])
    if tab not in MAP_COLS:
        raise HTTPException(400, f"unknown tab {tab!r} (valid: {sorted(MAP_COLS)})")
    if len(rows) > 2000:
        raise HTTPException(400, "too many rows in one POST")
    recs = []
    for i, r in enumerate(rows):
        rec = {c: _norm_cell(tab, c, r.get(c)) for c in MAP_COLS[tab]}
        if tab == "po_customer" and not rec["product_name"]:
            raise HTTPException(422, f"row{i}: product_name required")
        recs.append(rec)
    errs = gate_rows(tab, recs)
    if errs:
        raise HTTPException(422, {"rejected": errs[:25]})
    rel = body.get("rel_path"); folder = body.get("folder")
    with db() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT folder, rel_path, filename FROM documents WHERE id=%s", (doc_id,))
        drow = cur.fetchone()
        if not drow:
            raise HTTPException(404, "document not found")
        folder = folder or drow["folder"]
        rel = rel or drow["rel_path"]
        cur.execute(f"DELETE FROM {tab} WHERE document_id=%s", (doc_id,))
        written = 0
        for i, rec in enumerate(recs):
            rec.update(document_id=doc_id, folder=folder, rel_path=rel,
                       natural_key=f"{doc_id}|{tab}|{i}",
                       source_file=Path(str(drow["filename"] or "")).stem)
            if tab in ("faktur_pajak", "faktur_penjualan") and rec.get("source_page") is None:
                rec["source_page"] = 0
            if not str(rec.get("mapping_status") or "").strip():
                rec["mapping_status"] = "MAPPED"
            cols = list(rec)
            cur.execute(
                f"INSERT INTO {tab} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
                f"ON CONFLICT (natural_key) DO UPDATE SET "
                f"{', '.join(f'{x}=EXCLUDED.{x}' for x in cols if x != 'natural_key')}, updated_at=now()",
                [rec[x] for x in cols])
            written += 1
        cur.execute("""UPDATE map_tasks SET status='mapped', mapped_at=now(), error=NULL, updated_at=now()
                       WHERE document_id=%s""", (doc_id,))
        cur.execute("""UPDATE map_tasks SET rows_summary =
                       (SELECT COALESCE(jsonb_object_agg(k, v), '{}'::jsonb) FROM (
                          SELECT 'faktur_pajak' k, (SELECT count(*) FROM faktur_pajak WHERE document_id=%s) v
                          UNION SELECT 'faktur_penjualan', (SELECT count(*) FROM faktur_penjualan WHERE document_id=%s)
                          UNION SELECT 'po_customer', (SELECT count(*) FROM po_customer WHERE document_id=%s)
                          UNION SELECT 'tanda_terima', (SELECT count(*) FROM tanda_terima WHERE document_id=%s)) x)
                       WHERE document_id=%s""", (doc_id, doc_id, doc_id, doc_id, doc_id))
    return {"document_id": doc_id, "tab": tab, "written": written}


@app.post("/mapping/{doc_id}/note")
async def mapping_note(doc_id: str, request: Request):
    """Engine reports outcome without rows (no parser / needs review): status=review|failed|mapped + error."""
    body = await request.json()
    st = body.get("status") if body.get("status") in ("review", "failed", "mapped") else "review"
    with db() as c, c.cursor() as cur:
        cur.execute("UPDATE map_tasks SET status=%s, error=%s, updated_at=now() WHERE document_id=%s",
                    (st, str(body.get("error") or "")[:400], doc_id))
    return {"document_id": doc_id, "status": st}


@app.post("/mapping/{doc_id}/unmap")
def mapping_unmap(doc_id: str):
    """Clear this doc's mapped rows (all 4 tabs) and re-queue."""
    with db() as c, c.cursor() as cur:
        n = 0
        for t in MAP_COLS:
            cur.execute(f"DELETE FROM {t} WHERE document_id=%s", (doc_id,)); n += cur.rowcount
        cur.execute("""UPDATE map_tasks SET status='pending', error=NULL, claimed_at=NULL,
                       mapped_at=NULL, rows_summary='{}'::jsonb, updated_at=now() WHERE document_id=%s""", (doc_id,))
    return {"document_id": doc_id, "deleted_rows": n}


@app.get("/mapping/status")
def mapping_status():
    """Monitor aggregate for /view/mapping."""
    out = {"tasks": {}, "tabs": {}, "folders": []}
    with db() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT status, count(*) n FROM map_tasks GROUP BY 1")
        out["tasks"] = {r["status"]: r["n"] for r in cur.fetchall()}
        cur.execute("SELECT COALESCE(folder,'(no folder)') f, tab_guess, status, count(*) n "
                    "FROM map_tasks GROUP BY 1,2,3 ORDER BY 1,2")
        agg = {}
        for r in cur.fetchall():
            a = agg.setdefault(r["f"], {"folder": r["f"], "total": 0})
            a["total"] += r["n"]; a[r["status"]] = a.get(r["status"], 0) + r["n"]
        out["folders"] = sorted(agg.values(), key=lambda x: -x["total"])[:30]
        for t in MAP_COLS:
            cur.execute(f"SELECT count(*) n, "
                        f"count(*) FILTER (WHERE COALESCE(mapping_status,'') LIKE 'MAPPED%') m, "
                        f"count(*) FILTER (WHERE COALESCE(review_status,'') LIKE '%REVIEW%') rv FROM {t}")
            rr = cur.fetchone()
            out["tabs"][t] = {"rows": rr["n"], "mapped": rr["m"], "review": rr["rv"]}
    return out


@app.get("/mapping/rules")
def mapping_rules():
    """Rules digest — single source of truth for BOTH engines (hermes parsers + api LLM)."""
    out = {"tables": MAP_COLS}
    for name in ("po_rules.json", "issuers.json", "variants.json", "overrides.json"):
        p = KNOWLEDGE_DIR / name
        try:
            out[name.replace(".json", "")] = json.loads(p.read_text())
        except Exception:
            out[name.replace(".json", "")] = None
    return out


MAPPING_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Doc Pipeline — Mapping</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{font:14px/1.5 system-ui,sans-serif;margin:2rem auto;max-width:980px;padding:0 1rem;color:#1b1f24}
h1{font-size:1.3rem;font-weight:650}a{color:#0b6bcb;text-decoration:none}a:hover{text-decoration:underline}
.chips{display:flex;gap:.6rem;flex-wrap:wrap;margin:.6rem 0 1.2rem}
.chip{border:1px solid #d6dbe1;border-radius:8px;padding:.4rem .8rem;background:#fff}
.chip b{font-size:1.15rem;display:block}.chip span{color:#57606a;font-size:.8rem;text-transform:capitalize}
table{border-collapse:collapse;width:100%;margin-bottom:1.4rem}
th,td{border-bottom:1px solid #e8ebef;padding:.35rem .5rem;text-align:left;font-size:.86rem}
th{color:#57606a;font-weight:600}td.num{text-align:right;font-variant-numeric:tabular-nums}
.bar{height:6px;border-radius:3px;background:#e8ebef;overflow:hidden;min-width:120px}
.bar i{display:block;height:100%;background:#1a7f37}
.st{padding:.1rem .5rem;border-radius:99px;font-size:.78rem;font-weight:600;margin-right:.3rem;display:inline-block}
.st.mapped{background:#dcf5e3;color:#1a7f37}.st.pending{background:#eef1f5;color:#57606a}
.st.claimed{background:#fff3d6;color:#8a6100}.st.failed{background:#fde2e2;color:#b42318}
.st.review{background:#e7ecff;color:#3349a9}
h2{font-size:1rem;margin:1.4rem 0 .4rem;color:#333}</style></head><body>
<h1>Mapping monitor</h1>
<div style="color:#57606a">flow: upload &rarr; OCR &rarr; map &rarr; this queue &rarr; Google Sheet export &middot; <a href="/view">documents</a> &middot; <a href="/view/upload">upload</a></div>
<div class="chips" id="chips"></div><div id="tabs"></div><h2>Folders</h2><div id="folds"></div>
<h2>Needs attention (review/failed docs)</h2><div id="att"></div>
<p style="color:#8a929b;font-size:.8rem">auto-refresh 5s &middot; rules: <a href="/mapping/rules">/mapping/rules</a></p>
<script>
const TCOL={pending:1,claimed:1,mapped:1,failed:1,review:1};
async function showRows(id){const p=document.getElementById('rowdetail');p.style.display='block';p.textContent='loading...';
 const r=await (await fetch('/mapping/rows?document_id='+id)).json();
 let o='';for(const [t,rows] of Object.entries(r.tabs)){if(!rows.length)continue;o+=t+' ('+rows.length+')\n';
  for(const x of rows)o+='  '+JSON.stringify(x).slice(0,220)+'\n'}
 p.textContent=o||'(no rows yet — parser did not produce any for this doc)';}
async function tick(){try{
 const s=await (await fetch('/mapping/status')).json();
 let c='';for(const k of ['pending','claimed','mapped','review','failed']){if(s.tasks[k])c+='<div class="chip"><b>'+s.tasks[k]+'</b><span>'+k+'</span></div>'}
 let tr=Object.values(s.tasks).reduce((a,b)=>a+b,0);c+='<div class="chip"><b>'+tr+'</b><span>queued docs</span></div>';
 document.getElementById('chips').innerHTML=c;
 let t='<table><tr><th>target table</th><th class=num>rows</th><th class=num>MAPPED</th><th class=num>REVIEW</th></tr>';
 for(const [k,v] of Object.entries(s.tabs))t+='<tr><td>'+k+'</td><td class=num>'+v.rows+'</td><td class=num>'+v.mapped+'</td><td class=num>'+v.review+'</td></tr>';
 document.getElementById('tabs').innerHTML=t+'</table>';
 let f='<table><tr><th>folder</th><th class=num>docs</th><th>progress</th><th>states</th></tr>';
 for(const x of s.folders){const pct=x.total?Math.round(100*(x.mapped||0)/x.total):0;
  let st=Object.entries(x).filter(([k])=>TCOL[k]).map(([k,v])=>'<span class="st '+k+'">'+v+' '+k+'</span>').join('');
  f+='<tr><td>'+(x.folder||'').slice(0,44)+'</td><td class=num>'+x.total+'</td><td><div class="bar"><i style="width:'+pct+'%"></i></div></td><td>'+st+'</td></tr>';}
 document.getElementById('folds').innerHTML=f+'</table>';
 const a=await (await fetch('/mapping/rows?limit=12')).json();
 let ah='<table><tr><th>document</th><th>tab</th><th>status</th><th>note</th><th></th></tr>';
 for(const x of (a.attention||[])){ah+='<tr><td>'+(x.rel_path||x.filename||'').slice(-52)+'</td><td>'+x.tab_guess+'</td><td><span class="st '+x.status+'">'+x.status+'</span></td><td>'+(x.error||'').slice(0,40)+'</td><td><a href="/view/'+x.document_id+'">ocr</a> &middot; <a href="javascript:showRows(\''+x.document_id+'\')">rows</a></td></tr>'}
 document.getElementById('att').innerHTML=ah+'</table><pre id="rowdetail" style="max-height:300px;overflow:auto;background:#0b1020;color:#cfe3ff;padding:10px;border-radius:8px;font-size:11px;display:none"></pre>';
}catch(e){}}tick();setInterval(tick,5000);
</script></body></html>"""


@app.get("/mapping/rows")
def mapping_rows_list(document_id: str = "", tab: str = "", folder: str = "", limit: int = 200, offset: int = 0):
    """Drill-down: actual mapped rows. ?document_id=.. | ?tab=po_customer&folder=.. (newest first)."""
    tab = tab if tab in MAP_COLS else ""
    lim = max(1, min(int(limit), 1000))
    with db() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        if document_id:
            tabs = [tab] if tab else list(MAP_COLS)
            res = {}
            for t in tabs:
                cur.execute(f"SELECT * FROM {t} WHERE document_id=%s ORDER BY natural_key", (document_id,))
                res[t] = [dict(r) for r in cur.fetchall()]
            return {"document_id": document_id, "tabs": res}
        if tab:
            q = f"SELECT * FROM {tab} WHERE 1=1"; args = []
            if folder: q += " AND folder LIKE %s"; args.append(folder)
            cq = q.replace("SELECT *", "SELECT count(*)")
            cur.execute(cq, args)
            r0 = cur.fetchone()
            total = r0["count"] if isinstance(r0, dict) else r0[0]
            q += " ORDER BY natural_key LIMIT %s OFFSET %s"
            cur.execute(q, args + [lim, max(0, int(offset))])
            return {"tab": tab, "total": total, "offset": offset,
                    "rows": [dict(r) for r in cur.fetchall()]}
        cur.execute("""SELECT t.document_id, t.tab_guess, t.status, t.error, t.rows_summary,
                              d.folder, d.rel_path, d.filename
                       FROM map_tasks t JOIN documents d ON d.id=t.document_id
                       WHERE t.status IN ('review','failed')
                       ORDER BY t.updated_at DESC LIMIT %s""", (lim,))
        return {"attention": [dict(r) for r in cur.fetchall()]}


import csv as _csv

PAGE_PATH = Path(__file__).resolve().parent / "tables_page.html"


def _tables_html():
    js_const = ("const MAP_HEAD=" + json.dumps(MAP_SHEET_HEADERS, ensure_ascii=False) +
                ";const MAP_COLS=" + json.dumps(MAP_COLS, ensure_ascii=False) +
                ";const MAP_LABEL=" + json.dumps(MAP_LABEL, ensure_ascii=False) + ";")
    html = PAGE_PATH.read_text() if PAGE_PATH.exists() else "<pre>tables_page.html missing</pre>"
    return html.replace("/*JS_CONST*/", js_const)


@app.get("/mapping/export.csv")
def mapping_export_csv(tab: str = "", folder: str = ""):
    """Full-table CSV, headers exactly like the Google Sheet tab."""
    if tab not in MAP_COLS:
        raise HTTPException(400, f"tab must be one of {list(MAP_COLS)}")
    with db() as c, c.cursor() as cur:
        q = f"SELECT {', '.join(MAP_EXPORT_COLS[tab])} FROM {tab}"
        args = []
        if folder:
            q += " WHERE folder LIKE %s"; args.append(folder)
        q += " ORDER BY natural_key"
        cur.execute(q, args)
        data = cur.fetchall()
    buf = io.StringIO()
    w = _csv.writer(buf)
    w.writerow(MAP_SHEET_HEADERS[tab])
    for row in data:
        w.writerow(["" if v is None else v for v in row])
    fn = tab + ("_" + folder[:24].strip().replace(" ", "_") if folder else "") + ".csv"
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{fn}"'})


@app.get("/view/tables", response_class=HTMLResponse)
def view_tables():
    return _tables_html()


@app.get("/view/mapping", response_class=HTMLResponse)
def view_mapping():
    return MAPPING_HTML


# ---------------- mapping engine settings (like /settings/ocr — same Token Plan key style) ----
MAP_CONFIG_PATH = Path(os.environ.get("MAP_CONFIG_PATH", "/data/map_config.json"))
_map_cfg_cache = {"mtime": None, "cfg": None}

def load_map_config():
    """mtime-cached: UI save is live on the next call, Falls back to the
    OCR config's key/endpoint (same Token Plan) when mapping fields are absent."""
    try:
        mt = MAP_CONFIG_PATH.stat().st_mtime
    except OSError:
        mt = None
    if mt is None:
        cfg = {}
    elif _map_cfg_cache["mtime"] != mt:
        try:
            cfg = json.loads(MAP_CONFIG_PATH.read_text())
        except Exception:
            cfg = {}
        _map_cfg_cache.update(mtime=mt, cfg=cfg)
    else:
        cfg = _map_cfg_cache["cfg"]
    base = {"endpoint": os.environ.get("MAP_URL", ""),
            "model": os.environ.get("MAP_MODEL", ""),
            "key": os.environ.get("MAP_API_KEY", ""),
            "engine": os.environ.get("MAP_ENGINE", "hermes"),
            "auto": os.environ.get("MAP_AUTO", "0")}
    for k, v in base.items():
        if not cfg.get(k):
            cfg[k] = v
    if not cfg.get("key") or not cfg.get("endpoint"):
        ocr = load_ocr_config()
        cfg.setdefault("endpoint", ocr.get("endpoint", ""))
        cfg.setdefault("key", ocr.get("key", ""))
        cfg.setdefault("model", ocr.get("model", ""))
    cfg["model"] = cfg.get("model") or ocr.get("model", "") if "ocr" in dir() else cfg.get("model", "")
    return cfg


def save_map_config(cfg):
    cfg = {k: v for k, v in cfg.items() if k in ("endpoint", "model", "key", "engine", "auto")}
    if cfg.get("endpoint"):
        cfg["endpoint"] = normalize_chat_url(cfg["endpoint"])
    MAP_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = MAP_CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=1))
    os.chmod(tmp, 0o600)
    os.replace(tmp, MAP_CONFIG_PATH)
    _map_cfg_cache["mtime"] = None
    return cfg


def _map_rules_digest():
    """Compact rules brief for the prompt — the SAME knowledge files the deterministic
    parsers use (single source of truth)."""
    brief = []
    try:
        pr = json.loads((KNOWLEDGE_DIR / "po_rules.json").read_text())
        for k in ("ppn_rates", "total_formula", "issuer_is_customer", "samb_never_issuer_vendor",
                  "product_name_required", "no_guess"):
            if k in pr:
                brief.append(f"{k}: {json.dumps(pr[k], ensure_ascii=False)[:220]}")
    except Exception:
        pass
    try:
        iss = json.loads((KNOWLEDGE_DIR / "issuers.json").read_text())
        pairs = [f"{b}=>{v.get('pt')}" for b, v in list(iss.items())[:40] if isinstance(v, dict) and v.get("pt")]
        if pairs:
            brief.append("known brand->PT aliases: " + "; ".join(pairs)[:900])
    except Exception:
        pass
    return "\n".join(brief)


def map_prompt(tab, rel_path, text):
    cols = ", ".join(MAP_COLS[tab])
    return (
        "You map Indonesian supplier documents into one target table. Use ONLY values that appear in the text; "
        "leave a field null when unreadable — NEVER invent. Follow these rules exactly:\n"
        + _map_rules_digest() +
        f"\n\nTARGET TABLE {tab} columns: {cols}.\n"
        + ("PO rows MUST carry product_name; drop rows without a product name. "
           "po_issuer = the CUSTOMER company (PT), never SAMB/PT Sarana Abadi Makmur Bersama.\n"
           if tab == "po_customer" else "")
        + ("Ppn column format like '11%' or '1.1%'.\n" if tab == "po_customer" else "")
        + ("Dates dd/mm/yyyy or '16 September 2026' -> keep as written.\n" if tab in ("faktur_pajak", "tanda_terima") else "")
        + f"\nSource path hint: {rel_path}\n\nDOCUMENT TEXT:\n" + (text or "")[:24000] +
        "\n\nReply with JSON only: {\"rows\": [ {col: value or null, ...}, ... ]}"
    )


@app.post("/mapping/run")
async def mapping_run(request: Request):
    """Engine 'api': one document -> LLM with rules digest -> gated rows POST.
    Body: {"document_id":"...", "tab":"auto|po_customer|..."} or {"batch":N} for pending."""
    body = await request.json()
    cfg = load_map_config()
    if not cfg.get("key"):
        raise HTTPException(400, "map API key not configured — /view/settings")
    dids = [body["document_id"]] if body.get("document_id") else []
    if not dids:
        with db() as c, c.cursor() as cur:
            cur.execute("""SELECT t.document_id FROM map_tasks t JOIN documents d ON d.id=t.document_id
                           WHERE t.status IN ('pending','failed','review') AND d.status IN ('VALIDATED','DELIVERED')
                           ORDER BY t.created_at LIMIT %s""", (int(body.get("batch", 1)),))
            dids = [r[0] for r in cur.fetchall()]
    if not dids:
        return {"ran": 0, "note": "nothing pending"}
    done = []
    for did in dids:
        try:
            with db() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT standard_json, folder, rel_path, filename FROM documents WHERE id=%s", (did,))
                d = cur.fetchone()
            if not d:
                continue
            std = d["standard_json"] or {}
            text = "\n".join((p.get("text") or "") for p in sorted(std.get("pages", []), key=lambda x: x.get("page", 0)))
            tab = body.get("tab") or "auto"
            if tab == "auto":
                with db() as c, c.cursor() as cur:
                    cur.execute("SELECT tab_guess FROM map_tasks WHERE document_id=%s", (did,))
                    r = cur.fetchone()
                tab = r[0] if r else "other"
            if tab not in MAP_COLS:
                await _post_note(did, "review", f"no target tab guess for {d['filename']}")
                continue
            with db() as c3, c3.cursor() as cur3:
                cur3.execute("UPDATE map_tasks SET status='claimed', engine='api', attempts=attempts+1, updated_at=now() WHERE document_id=%s AND status IN ('pending','failed','review')", (did,))
            prompt = map_prompt(tab, d["rel_path"], text)
            payload = json.dumps({"model": cfg["model"], "temperature": 0,
                                  "messages": [{"role": "user", "content": prompt}]}).encode()
            req = urllib.request.Request(normalize_chat_url(cfg["endpoint"]), data=payload,
                                         headers={"Content-Type": "application/json",
                                                  "Authorization": f"Bearer {cfg['key']}"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                out = json.loads(resp.read())
            content = out["choices"][0]["message"]["content"]
            m = re.search(r"\{.*\}", content, re.S)
            rows = json.loads(m.group(0))["rows"] if m else []
            if not rows:
                await _post_note(did, "review", "engine returned no rows")
                done.append({"document_id": did, "tab": tab, "written": 0, "note": "no rows"})
                continue
            w = await _write_rows(did, tab, rows)
            done.append({"document_id": did, "tab": tab, "written": w})
        except Exception as e:
            await _post_note(did, "failed", str(e)[:300])
            done.append({"document_id": did, "error": str(e)[:200]})
    return {"ran": len(done), "results": done}


async def _post_note(did, status, error):
    with db() as c, c.cursor() as cur:
        cur.execute("UPDATE map_tasks SET status=%s, error=%s, updated_at=now() WHERE document_id=%s",
                    (status, error[:400], did))


async def _write_rows(did, tab, rows):
    """Reuse the gated writer by calling the endpoint function in-process."""
    from fastapi import Request as _R  # noqa - body passed directly
    recs, errs = [], []
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            errs.append(f"row{i}: not object"); continue
        rec = {c: _norm_cell(tab, c, r.get(c)) for c in MAP_COLS[tab]}
        if tab == "po_customer" and not rec["product_name"]:
            errs.append(f"row{i}: product_name required"); continue
        recs.append(rec)
    errs += gate_rows(tab, recs)
    if errs and not recs:
        raise HTTPException(422, {"rejected": errs[:25]})
    with db() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT folder, rel_path, filename FROM documents WHERE id=%s", (did,))
        drow = cur.fetchone()
        cur.execute(f"DELETE FROM {tab} WHERE document_id=%s", (did,))
        written = 0
        for i, rec in enumerate(recs):
            rec.update(document_id=did, folder=drow["folder"], rel_path=drow["rel_path"],
                       natural_key=f"{did}|{tab}|{i}", source_file=Path(str(drow["filename"] or "")).stem)
            if tab in ("faktur_pajak", "faktur_penjualan") and rec.get("source_page") is None:
                rec["source_page"] = 0
            if not str(rec.get("mapping_status") or "").strip():
                rec["mapping_status"] = "MAPPED"
            cols = list(rec)
            cur.execute(
                f"INSERT INTO {tab} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
                f"ON CONFLICT (natural_key) DO UPDATE SET "
                f"{', '.join(f'{x}=EXCLUDED.{x}' for x in cols if x != 'natural_key')}, updated_at=now()",
                [rec[x] for x in cols])
            written += 1
        cur.execute("""UPDATE map_tasks SET status='mapped', mapped_at=now(), error=NULL, updated_at=now()
                       WHERE document_id=%s""", (did,))
        cur.execute("""UPDATE map_tasks SET rows_summary =
                       (SELECT COALESCE(jsonb_object_agg(k, v), '{}'::jsonb) FROM (
                          SELECT 'faktur_pajak' k, (SELECT count(*) FROM faktur_pajak WHERE document_id=%s) v
                          UNION SELECT 'faktur_penjualan', (SELECT count(*) FROM faktur_penjualan WHERE document_id=%s)
                          UNION SELECT 'po_customer', (SELECT count(*) FROM po_customer WHERE document_id=%s)
                          UNION SELECT 'tanda_terima', (SELECT count(*) FROM tanda_terima WHERE document_id=%s)) x)
                       WHERE document_id=%s""", (did, did, did, did, did))
    return written


@app.get("/settings/map")
def settings_map_get():
    cfg = dict(load_map_config())
    cfg["key"] = mask_key(cfg.get("key", ""))
    return cfg


@app.post("/settings/map")
async def settings_map_post(request: Request):
    body = await request.json()
    if not _pin_ok(body):
        raise HTTPException(403, "wrong PIN")
    cfg = dict(load_map_config())
    for k in ("endpoint", "model", "engine", "auto"):
        if k in body:
            cfg[k] = body[k]
    if body.get("key") and not str(body["key"]).startswith(("****", "(empty)")):
        cfg["key"] = body["key"]
    cfg = save_map_config(cfg)
    cfg_out = dict(cfg); cfg_out["key"] = mask_key(cfg.get("key", ""))
    return {"saved": True, "config": cfg_out}


@app.post("/settings/map/test")
async def settings_map_test(request: Request):
    """Dry-run the mapping engine on ONE real document (no DB write) — like /settings/ocr/test."""
    import urllib.error
    body = await request.json()
    cfg = {**load_map_config(), **{k: v for k, v in body.items() if k in ("endpoint", "model", "key")
                                   and not str(v).startswith(("****", "(empty)"))}}
    if not cfg.get("key"):
        raise HTTPException(400, "no API key")
    did = body.get("document_id")
    if not did:
        with db() as c, c.cursor() as cur:
            cur.execute("SELECT document_id FROM map_tasks WHERE status IN ('pending','failed','review') ORDER BY created_at LIMIT 1")
            r = cur.fetchone(); did = r[0] if r else None
    if not did:
        raise HTTPException(400, "no pending document to test on")
    with db() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT d.standard_json, t.rel_path, t.folder, t.tab_guess FROM map_tasks t LEFT JOIN documents d ON d.id=t.document_id WHERE t.document_id=%s", (did,))
        row = cur.fetchone()
    std = (row or {}).get("standard_json") or {}
    text = "\n".join((p.get("text") or "") for p in sorted(std.get("pages", []) or [], key=lambda x: x.get("page", 0)))
    tab = row["tab_guess"] if row else "po_customer"
    if tab not in MAP_COLS:
        tab = "po_customer"
    prompt = map_prompt(tab, (row or {}).get("rel_path"), text)
    t0 = time.time()
    payload = json.dumps({"model": cfg.get("model") or "qwen3.8-flash", "temperature": 0,
                          "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(normalize_chat_url(cfg["endpoint"]), data=payload,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {cfg['key']}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            out = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        hint = {401: "key rejected (401)", 404: "model not available (404)", 429: "rate limited (429)"}.get(e.code, str(e.code))
        raise HTTPException(502, f"map endpoint said {hint}")
    except Exception as e:
        raise HTTPException(502, f"cannot reach endpoint: {type(e).__name__}: {str(e)[:120]}")
    content = out["choices"][0]["message"]["content"]
    m = re.search(r"\{.*\}", content, re.S)
    rows = json.loads(m.group(0)).get("rows", []) if m else []
    recs = []
    for r in rows:
        if isinstance(r, dict):
            rec = {c: _norm_cell(tab, c, r.get(c)) for c in MAP_COLS[tab]}
            if tab == "po_customer" and not rec["product_name"]:
                continue
            recs.append(rec)
    errs = gate_rows(tab, recs)
    usage = out.get("usage", {})
    return {"ok": True, "document_id": did, "tab": tab, "seconds": round(time.time() - t0, 1),
            "rows_returned": len(rows), "rows_valid": len(recs),
            "gate_rejections": errs[:10], "sample": recs[:3],
            "tokens": {"prompt": usage.get("prompt_tokens"), "completion": usage.get("completion_tokens")},
            "rules_digest_chars": len(_map_rules_digest()), "prompt_chars": len(prompt)}


@app.post("/documents/{doc_id}/correct")
async def correct_fields(doc_id: str, request: Request):
    """Review loop entry: user/agent posts corrected extracted fields; re-validate;
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


OCR_MAX_PAGES = int(os.environ.get("OCR_MAX_PAGES", "0"))  # 0 = process every page
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
            {"type": "text", "text": "Extract ALL text from this document image exactly as written, preserving the document's visual reading order and table row/column structure. This is a Faktur Penjualan: preserve headers and every line-item field, including Harga, Disc 1, Disc 2, Disc 3, Disc 4, Disc 5, Jumlah, subtotal, DPP, PPN, and TOTAL. Include printed AND handwritten content. Transcribe handwritten numbers verbatim; do not normalize, autocorrect, infer, or replace digits. In particular, do not change a handwritten 366000 into 266000. If a character is genuinely unreadable, write [unclear] rather than guessing. Output only the transcription."}]}]}).encode()
    req = ur.Request(cfg["endpoint"], data=body, method="POST")
    req.add_header("Authorization", "Bearer " + cfg["key"])
    req.add_header("Content-Type", "application/json")
    try:
        with ur.urlopen(req, timeout=120) as r:
            d = json.load(r)
    except ur.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")[:500]
        except Exception:
            detail = ""
        raise RuntimeError(f"OCR API HTTP {e.code}: {detail}") from e
    t = d["choices"][0]["message"]["content"]
    if isinstance(t, list):
        t = "\n".join(x.get("text", "") for x in t)
    return (t or "").strip()

# backwards-compat alias (older code paths)
qwen_vision_ocr = api_vision_ocr


def vision_ask_png(png_bytes: bytes, prompt: str, cfg=None) -> str:
    """One vision call with an ARBITRARY prompt (deep-verify stages). Same config, no retrain."""
    import urllib.request as ur
    cfg = cfg or load_ocr_config()
    if not cfg.get("key"):
        raise HTTPException(500, "OCR API key not configured — set it at /view/settings")
    body = json.dumps({
        "model": cfg["model"], "max_tokens": 2200,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(png_bytes).decode()}},
            {"type": "text", "text": prompt}]}]}).encode()
    req = ur.Request(cfg["endpoint"], data=body, method="POST")
    req.add_header("Authorization", "Bearer " + cfg["key"])
    req.add_header("Content-Type", "application/json")
    with ur.urlopen(req, timeout=180) as r:
        d = json.load(r)
    t = d["choices"][0]["message"]["content"]
    if isinstance(t, list):
        t = "\n".join(x.get("text", "") for x in t)
    return (t or "").strip()


class VisionAskIn(BaseModel):
    page: int
    prompt: str
    dpi: int = 400


@app.post("/documents/{doc_id}/vision_ask")
def post_vision_ask(doc_id: str, body: VisionAskIn):
    """Render ONE page at high DPI and ask the vision model anything. Evidence tool for
    deep_verify.py loops. Reads the STORED original (never cached OCR text)."""
    with db() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT stored_path,mime FROM documents WHERE id=%s", (doc_id,))
        d = cur.fetchone()
    if not d or not os.path.exists(d["stored_path"]):
        raise HTTPException(404, "file not found")
    cfg = load_ocr_config()
    td = tempfile.mkdtemp(prefix="vask-")
    try:
        if d["mime"] == "application/pdf":
            prefix = os.path.join(td, "pg")
            subprocess.run(["pdftoppm", "-png", "-r", str(body.dpi), "-f", str(body.page),
                            "-l", str(body.page), d["stored_path"], prefix], check=True, timeout=240)
            pngs = glob.glob(prefix + "*.png")
            if not pngs:
                raise HTTPException(422, f"page {body.page} rendered nothing")
            png = pngs[0]
        elif (d["mime"] or "").startswith("image/"):
            png = d["stored_path"]
        else:
            raise HTTPException(415, "vision_ask supports pdf pages and images")
        with open(png, "rb") as fh:
            txt = vision_ask_png(fh.read(), body.prompt, cfg)
        return {"document_id": doc_id, "page": body.page, "text": txt}
    finally:
        shutil.rmtree(td, ignore_errors=True)


def _sanitize_ocr_text(text: str) -> str:
    """Reject model hallucination on blank/near-blank pages."""
    text = (text or "").strip()
    if not text:
        return "[BLANK_PAGE]"
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    uncertain = sum(1 for line in lines if line.lower() in {"[unclear]", "unclear", "[unknown]"})
    meaningful = " ".join(line for line in lines if line.lower() not in {"[unclear]", "unclear", "[unknown]"})
    if uncertain >= 5 and len(meaningful) < 180:
        return "[BLANK_OR_UNREADABLE_PAGE]"
    return text


def _needs_numeric_verification(text: str) -> bool:
    """Verify only pages likely to contain financial/table numbers."""
    upper = (text or "").upper()
    markers = (
        "FAKTUR PENJUALAN", "FAKTUR PAJAK", "INVOICE", "DPP", "PPN",
        "TOTAL", "SUBTOTAL", "HARGA", "DISC", "DISKON", "QTY",
        "JUMLAH", "PURCHASE ORDER", "NO. FAKTUR", "NILAI SETELAH PPN"
    )
    return any(marker in upper for marker in markers)


def api_numeric_verify(png_bytes: bytes, ocr_text: str, cfg=None) -> dict:
    """Second-pass numeric verification against the page image."""
    import urllib.request as ur
    cfg = cfg or load_ocr_config()
    prompt = """You are a strict numeric verifier for an Indonesian Faktur Penjualan.
Compare the page image against the OCR transcription below. Return JSON only:
{"status":"PASS|REVIEW", "numbers":[{"label":"", "value_as_seen":"", "confidence":"high|medium|low", "handwritten":false}], "line_items":[{"code":"", "qty":"", "unit_price":"", "disc_1":"", "disc_2":"", "disc_3":"", "disc_4":"", "disc_5":"", "amount":""}], "totals":{"subtotal":"", "dpp":"", "ppn":"", "total":""}, "arithmetic":{"status":"PASS|FAIL|NOT_CHECKED", "notes":""}, "discrepancies":["..."]}
Rules:
- Inspect the image, not only the OCR text.
- Copy digits exactly as visible; do not autocorrect or infer.
- Preserve Indonesian separators and leading zeros.
- For handwritten digits, use confidence=low and status=REVIEW if any digit is uncertain; never substitute a plausible digit.
- Include every visible line item and every Disc 1 through Disc 5 field.
- If the page is blank or has no financial table, return status PASS, empty numbers and line_items, and arithmetic NOT_CHECKED.
- Mark REVIEW for any OCR/image mismatch, missing numeric field, or arithmetic inconsistency.

OCR TRANSCRIPTION:
""" + (ocr_text or "")
    body = json.dumps({
        "model": cfg["model"], "max_tokens": 3000,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(png_bytes).decode()}},
            {"type": "text", "text": prompt}
        ]}]
    }).encode()
    req = ur.Request(cfg["endpoint"], data=body, method="POST")
    req.add_header("Authorization", "Bearer " + cfg["key"])
    req.add_header("Content-Type", "application/json")
    with ur.urlopen(req, timeout=180) as r:
        d = json.load(r)
    t = d["choices"][0]["message"]["content"]
    if isinstance(t, list):
        t = "\n".join(x.get("text", "") for x in t)
    return _parse_json_loose(t)


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

TABLE_ITEMS_PROMPT = """Ekstrak SEMUA baris item tabel dari teks OCR berikut.
Keluarkan hanya JSON object: {"line_items":[{"description":null,"code":null,"qty":null,"uom":null,"unit_price":null,"amount":null,"page":null}]}
Aturan:
- Ambil setiap baris barang yang benar-benar terbaca; jangan meringkas menjadi 'lain-lain'.
- Pertahankan halaman sumber dari penanda HALAMAN.
- Jangan menggabungkan item dari nomor SO/faktur berbeda.
- Nilai yang tidak terbaca harus null; jangan mengarang.
- Hanya baris item, jangan masukkan subtotal, pajak, total, header, atau catatan.
Keluarkan HANYA JSON."""


def llm_text(prompt: str, cfg, max_tokens: int = 4000) -> str:
    import urllib.request as ur
    body = json.dumps({"model": cfg["model"], "max_tokens": max_tokens,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = ur.Request(cfg["endpoint"], data=body, method="POST")
    req.add_header("Authorization", "Bearer " + cfg["key"])
    req.add_header("Content-Type", "application/json")
    last_error = None
    for attempt in range(1, 4):
        try:
            with ur.urlopen(req, timeout=int(os.environ.get("MAP_TIMEOUT", "120"))) as r:
                d = json.load(r)
            t = d["choices"][0]["message"]["content"]
            if isinstance(t, list):
                t = "\n".join(x.get("text", "") for x in t)
            return t or ""
        except Exception as e:
            last_error = e
            if attempt < 3:
                print("LLM-MAP-RETRY", attempt, str(e)[:180], flush=True)
                time.sleep(5 * attempt)
    raise last_error


AGENT_PLAN_PROMPT = """Anda adalah planner ekstraksi tabel dokumen.
Buat rencana kerja dari metadata halaman OCR. Keluarkan JSON saja:
{"groups":[{"pages":[1,2,3],"reason":"...","table_expected":true}]}
Aturan:
- Kelompokkan halaman berurutan maksimal 3 halaman per group.
- Jangan melewati halaman.
- Jika halaman tampak header/total tanpa tabel, tetap masukkan bila terkait dokumen.
- Jangan mengarang nomor halaman.
"""


def _agent_validate_items(items, page_numbers):
    issues = []
    seen = set()
    for i, item in enumerate(items or [], 1):
        if not isinstance(item, dict):
            issues.append(f"item {i} bukan object")
            continue
        key = (item.get("page"), item.get("code"), (item.get("description") or "").strip().lower())
        if key in seen and key != (None, None, ""):
            issues.append(f"duplikat item pada halaman {item.get('page')}")
        seen.add(key)
        if item.get("page") is not None:
            try:
                if int(item["page"]) not in page_numbers:
                    issues.append(f"page {item.get('page')} di luar chunk")
            except Exception:
                issues.append(f"page {item.get('page')} bukan angka")
    return issues


def _agent_plan_pages(std, cfg):
    pages = std.get("pages") or []
    page_text = "\n".join(f"PAGE {p.get('page', i+1)}: {(p.get('text') or '')[:500]}"
                            for i, p in enumerate(pages))
    try:
        raw = llm_text(AGENT_PLAN_PROMPT + "\n\n" + page_text[:12000], cfg, max_tokens=1200)
        plan = _parse_json_loose(raw)
        groups = plan.get("groups") or []
        valid = []
        all_pages = {int(p.get("page", i + 1)) for i, p in enumerate(pages)}
        for group in groups:
            nums = [int(n) for n in (group.get("pages") or []) if int(n) in all_pages]
            if nums:
                valid.append({"pages": nums[:3], "reason": group.get("reason", "agent plan")})
        if valid:
            return valid
    except Exception as e:
        print("AGENT-MAP-PLAN-FAILED", str(e)[:180], flush=True)
    nums = [int(p.get("page", i + 1)) for i, p in enumerate(pages)]
    return [{"pages": nums[i:i + 3], "reason": "bounded fallback chunk"}
            for i in range(0, len(nums), 3)]


def _agent_extract_group(std, cfg, group, repair=""):
    pages = std.get("pages") or []
    wanted = set(group["pages"])
    selected = [p for i, p in enumerate(pages) if int(p.get("page", i + 1)) in wanted]
    text = "\n".join(f"--- HALAMAN {p.get('page', i+1)} ---\n{p.get('text','')}"
                      for i, p in enumerate(selected))
    prompt = TABLE_ITEMS_PROMPT
    if repair:
        prompt += "\nPERBAIKAN WAJIB: " + repair
    raw = llm_text(prompt + "\n\n=== TEKS OCR ===\n" + text[:14000], cfg, max_tokens=2200)
    out = _parse_json_loose(raw)
    return out.get("line_items") or []


def _agent_map_line_items(std, cfg):
    """Bounded agent loop: plan -> extract -> validate -> repair -> merge."""
    plan = _agent_plan_pages(std, cfg)
    print("AGENT-MAP-PLAN", len(plan), "groups", flush=True)
    merged = []
    trace = []
    max_retries = int(os.environ.get("MAPPING_AGENT_RETRIES", "2"))
    for index, group in enumerate(plan, 1):
        items, issues, repair = [], [], ""
        for attempt in range(1, max_retries + 1):
            print("AGENT-MAP-STEP", index, "extract", attempt, group["pages"], flush=True)
            items = _agent_extract_group(std, cfg, group, repair)
            issues = _agent_validate_items(items, set(group["pages"]))
            if not issues:
                break
            repair = "; ".join(issues[:5])
            print("AGENT-MAP-REPAIR", index, attempt, repair, flush=True)
        merged.extend(items)
        trace.append({"group": group["pages"], "attempts": attempt, "issues": issues})
    unique = []
    seen = set()
    for item in merged:
        key = (item.get("page"), item.get("code"), (item.get("description") or "").strip().lower(), item.get("qty"))
        if key not in seen:
            seen.add(key); unique.append(item)
    return unique, {"planner": "llm", "groups": len(plan), "trace": trace}


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
    print("AUTO-CLASSIFY-START", filename, len(text), "chars", flush=True)
    raw = llm_text(prompt, load_ocr_config(), max_tokens=500)
    print("AUTO-CLASSIFY-DONE", filename, flush=True)
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
    prompt = INVOICE_MAP_PROMPT + "\n\n=== TEKS DOKUMEN ===\n" + text[:10000]
    print("AUTO-MAP-LLM-START", len(text), "chars", flush=True)
    raw = llm_text(prompt, cfg, max_tokens=1800)
    print("AUTO-MAP-LLM-DONE", flush=True)
    out = _parse_json_loose(raw)
    if std.get("kind") == "pdf" and (std.get("pages") or []):
        try:
            page_items, agent_trace = _agent_map_line_items(std, cfg)
            if page_items:
                out["line_items"] = page_items
                out["_mapping_agent"] = agent_trace
                print("AGENT-MAP-DONE", len(page_items), "rows", flush=True)
        except Exception as e:
            print("AUTO-MAP-LINE-ITEMS-FAILED", str(e)[:180], flush=True)
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
        cur.execute("SELECT id FROM invoice_rows WHERE document_id=%s", (doc_id,))
        header = cur.fetchone()
        header_id = header[0] if header else None
        cur.execute("DELETE FROM invoice_line_items WHERE document_id=%s", (doc_id,))
        item_sql = """INSERT INTO invoice_line_items
            (document_id, invoice_row_id, line_no, source_page, item_code,
             description, quantity, uom, unit_price, amount, raw_item, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,now())
            ON CONFLICT (document_id,line_no) DO UPDATE SET
              invoice_row_id=EXCLUDED.invoice_row_id,
              source_page=EXCLUDED.source_page, item_code=EXCLUDED.item_code,
              description=EXCLUDED.description, quantity=EXCLUDED.quantity,
              uom=EXCLUDED.uom, unit_price=EXCLUDED.unit_price,
              amount=EXCLUDED.amount, raw_item=EXCLUDED.raw_item, updated_at=now()"""
        for line_no, item in enumerate(m.get("line_items") or [], 1):
            def _item_num(value):
                if value is None or (isinstance(value, str) and not value.strip()):
                    return None
                try:
                    return float(str(value).replace(",", ""))
                except Exception:
                    return None
            cur.execute(item_sql, (
                doc_id, header_id, line_no, item.get("page"), item.get("code"),
                item.get("description"), _item_num(item.get("qty")), item.get("uom"),
                _item_num(item.get("unit_price")), _item_num(item.get("amount")),
                json.dumps(item, ensure_ascii=False)))
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


@app.get("/invoice-line-items")
def list_invoice_line_items(document_id: str = "", limit: int = 2000):
    """Normalized OCR table: one database row per mapped line item."""
    lim = min(max(1, limit), 10000)
    with db() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        if document_id:
            cur.execute("SELECT * FROM invoice_line_items WHERE document_id=%s ORDER BY source_page NULLS LAST, line_no LIMIT %s",
                        (document_id, lim))
        else:
            cur.execute("SELECT * FROM invoice_line_items ORDER BY document_id, source_page NULLS LAST, line_no LIMIT %s", (lim,))
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


def finalize_status(doc_id: str):
    """Recompute validation + status from stored standard_json (used by chunked/resumed OCR)."""
    with db() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM documents WHERE id=%s", (doc_id,))
        d = cur.fetchone()
        if not d or not d["standard_json"]:
            return
        std = d["standard_json"]
        env = {"schema_version": std.get("schema_version", "1.0"), "document_id": doc_id,
               "filename": d["filename"], "sha256": d["sha256"], "extracted": std}
        ok, flags = validate(env)
        status = "VALIDATED" if ok else "FLAGGED"
        cur.execute("UPDATE documents SET standard_json=%s::jsonb, validation=%s::jsonb, status=%s, updated=now() WHERE id=%s",
                    (json.dumps(std), json.dumps({"passed": ok, "flagged_fields": flags}), status, doc_id))
        c.commit()
        print("AUTO-OCR-FINALIZE", doc_id[:8], status, flush=True)
    if status == "VALIDATED":
        _map_enqueue([doc_id])  # late/asynchronous OCR completion still queues mapping
    return status


@app.post("/documents/{doc_id}/ocr")
def run_ocr(doc_id: str, lang: str = "eng", engine: str = "", max_pages: int = 0, dpi: int = 0, start_page: int = 1):
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
        total_pages = 1
        ocr_cfg = load_ocr_config() if engine == "qwen" else None
        if engine == "qwen" and not ocr_cfg.get("key"):
            raise HTTPException(500, "OCR API key not configured — set it at /view/settings")
        try:
            if mime == "application/pdf":
                info = subprocess.run(["pdfinfo", path], capture_output=True, text=True).stdout
                start_page = max(1, int(start_page))
                requested_pages = max_pages if max_pages > 0 else OCR_MAX_PAGES
                total_pages = 0
                for ln in info.splitlines():
                    if ln.startswith("Pages"):
                        total_pages = int(ln.split(":")[-1].strip())
                        break
                if total_pages <= 0:
                    raise RuntimeError("PDF page count unavailable")
                if start_page > total_pages:
                    raise HTTPException(400, f"start_page {start_page} exceeds total pages {total_pages}")
                if requested_pages <= 0:
                    requested_pages = total_pages - start_page + 1
                end_page = min(total_pages, start_page + requested_pages - 1)
                prefix_dir = tempfile.mkdtemp(prefix="ocrr-")
                def chunk_render_pages(all_items):
                    """Render PNGs in chunks (pdftoppm 300s cap) for given page numbers."""
                    out = {}
                    CH = 80
                    for ci in range(0, len(all_items), CH):
                        cnums = all_items[ci:ci + CH]
                        td = tempfile.mkdtemp(dir=prefix_dir)
                        prefix = os.path.join(td, "pg")
                        subprocess.run(["pdftoppm", "-png", "-r", str(dpi if dpi > 0 else int(OCR_DPI)),
                                        "-f", str(cnums[0]), "-l", str(cnums[-1]), path, prefix],
                                       check=True, timeout=300)
                        for pngp in glob.glob(prefix + "*.png"):
                            out[int(os.path.basename(pngp).rsplit("-", 1)[-1].split(".", 1)[0])] = pngp
                        for c in cnums:  # pdftoppm numbering sanity
                            if c not in out:
                                raise RuntimeError(f"render missing page {c}")
                    return out
                # RESUME: keep already-OCR'd pages (same engine) to save time/cost on retries
                prior_pages = {}
                if std.get("ocr_engine") == engine and std.get("kind") == "pdf":
                    for p in std.get("pages", []):
                        t = (p.get("text") or "").strip()
                        # keep substantive pages AND engine-confirmed blank pages (avoid re-OCR loop)
                        if t and (len(t) >= 20 or t.startswith("[BLANK_PAGE]")):
                            try: prior_pages[int(p["page"])] = p
                            except (TypeError, ValueError): pass
                if engine == "qwen":
                    todo = [i for i in range(start_page, end_page + 1) if i not in prior_pages]
                else:
                    todo = list(range(start_page, end_page + 1))
                    prior_pages = {}
                page_results = dict(prior_pages)
                print("AUTO-OCR", doc_id[:8], f"RESUME skip={len(prior_pages)} todo={len(todo)}", flush=True)
                def render_and_ocr(todo_list):
                    png_map = chunk_render_pages(todo_list)
                    return [(n, png_map[n]) for n in sorted(png_map)]

                def process_page(item):
                    i, pg = item
                    print("AUTO-OCR-PAGE", doc_id[:8], f"{i}/{total_pages} START", flush=True)
                    last_error = None
                    verification = None
                    for attempt in range(1, 4):
                        try:
                            if engine == "qwen":
                                with open(pg, "rb") as fh:
                                    png_bytes = fh.read()
                                txt = api_vision_ocr(png_bytes, ocr_cfg)
                            else:
                                png_bytes = None
                                txt = _tesseract(pg, lang)
                            txt = _sanitize_ocr_text(txt)
                            if engine == "qwen" and os.environ.get("OCR_NUMERIC_VERIFY", "1") == "1" and not txt.startswith("[BLANK") and _needs_numeric_verification(txt):
                                try:
                                    verification = api_numeric_verify(png_bytes, txt, ocr_cfg)
                                except Exception as ve:
                                    verification = {"status": "REVIEW", "discrepancies": ["numeric verification failed: " + str(ve)[:180]]}
                            last_error = None
                            break
                        except Exception as e:
                            last_error = e
                            if attempt < 3:
                                print("AUTO-OCR-PAGE", doc_id[:8], f"{i}/{total_pages} RETRY {attempt}", str(e)[:180], flush=True)
                                time.sleep(5 * attempt)
                    if last_error is not None:
                        raise last_error
                    result = {"page": i, "text": txt, "numeric_verification": verification}
                    print("AUTO-OCR-PAGE", doc_id[:8], f"{i}/{total_pages} DONE", len(txt), "chars", flush=True)
                    return result

                worker_count = max(1, min(int(os.environ.get("OCR_WORKERS", "4")), max(len(todo), 1)))
                def persist_progress():
                    ptext = [page_results[k] for k in sorted(page_results)]
                    progress_std = dict(std)
                    progress_std["kind"] = "pdf"
                    progress_std["ocr_engine"] = engine
                    if engine == "qwen":
                        progress_std["ocr_model"] = ocr_cfg["model"]
                    progress_std["ocr_workers"] = worker_count
                    progress_std["pages"] = list(ptext)
                    progress_std["ocr_progress"] = {
                        "status": "RUNNING", "completed_pages": len(page_results),
                        "total_pages": total_pages, "completed_chars": sum(len(p["text"]) for p in ptext),
                        "numeric_verified_pages": sum(1 for p in ptext if (p.get("numeric_verification") or {}).get("status") == "PASS"),
                        "numeric_review_pages": sum(1 for p in ptext if (p.get("numeric_verification") or {}).get("status") == "REVIEW"),
                        "updated_at": datetime.datetime.utcnow().isoformat() + "Z"
                    }
                    cur.execute("UPDATE documents SET standard_json=%s::jsonb, updated=now() WHERE id=%s",
                                (json.dumps(progress_std), doc_id))
                    c.commit()

                try:
                    CH = 80
                    for ci in range(0, len(todo), CH):
                        chunk = todo[ci:ci + CH]
                        items = render_and_ocr(chunk)
                        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="ocr") as pool:
                            futures = {pool.submit(process_page, item): item[0] for item in items}
                            for future in as_completed(futures):
                                result = future.result()
                                page_results[result["page"]] = result
                                persist_progress()
                finally:
                    shutil.rmtree(prefix_dir, ignore_errors=True)
                pages_text = [page_results[k] for k in sorted(page_results)]
                std["kind"] = "pdf"
                std["ocr_engine"] = engine
                if engine == "qwen": std["ocr_model"] = ocr_cfg["model"]
                std["ocr_workers"] = worker_count
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
            base_pages = {int(p["page"]): dict(p) for p in std.get("pages", [])}
            for p in pages_text:
                page_no = int(p["page"])
                existing = base_pages.get(page_no, {})
                t = existing.get("text", "")
                if prev_was_ocr:
                    keep = p["text"].strip()
                else:
                    keep = (t + "\n" + p["text"]).strip() if len(t.strip()) < len(p["text"].strip()) else t.strip()
                merged_page = {"page": page_no, "text": keep}
                if "numeric_verification" in p:
                    merged_page["numeric_verification"] = p["numeric_verification"]
                base_pages[page_no] = merged_page
            std["pages"] = [base_pages[k] for k in sorted(base_pages)]
            std["page_count"] = max(std.get("page_count", 0), total_pages)
        else:
            std["ocr"] = pages_text[0]["text"]

        full_document_run = start_page == 1 and len(pages_text) == total_pages
        numeric_statuses = [p.get("numeric_verification") or {} for p in pages_text]
        std["ocr_progress"] = {
            "status": "COMPLETED" if full_document_run else "PARTIAL",
            "completed_pages": len(pages_text),
            "total_pages": total_pages,
            "completed_chars": sum(len(p.get("text", "")) for p in pages_text),
            "numeric_verified_pages": sum(1 for v in numeric_statuses if v.get("status") == "PASS"),
            "numeric_review_pages": sum(1 for v in numeric_statuses if v.get("status") == "REVIEW"),
            "updated_at": datetime.datetime.utcnow().isoformat() + "Z"
        }

        # Auto-map is intentionally disabled in OCR-only mode (MAP_AUTO=0).
        if os.environ.get("MAP_AUTO", "0") == "1":
            try:
                children = _split_parent_if_needed(doc_id, std, d["filename"])
                if children:
                    print("AUTO-SPLIT", doc_id[:8], "children:", len(children), flush=True)
                    valid_children, flagged_children = [], []
                    with db() as c2, c2.cursor() as cur2:
                        for child_id in children:
                            cur2.execute("SELECT status FROM documents WHERE id=%s", (child_id,))
                            (flagged_children if (cur2.fetchone() or [""])[0] == "FLAGGED" else valid_children).append(child_id)
                    for child_id in valid_children: _map_one_now(child_id)
                    if flagged_children: _auto_ocr_bg(flagged_children)
                else:
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
        mapping_error = isinstance(std.get("mapped"), dict) and bool(std["mapped"].get("error"))
        if mapping_error:
            ok = False
            flags = list(flags) + [{"rule": "mapping completed", "field": "mapped", "value": "error"}]
        deliverable = ok and os.environ.get("MAP_AUTO", "0") == "1" and not bool(std.get("parts_created"))
        final_status = "SPLIT_PARENT" if std.get("parts_created") else ("VALIDATED" if ok else "FLAGGED")
        cur.execute("UPDATE documents SET standard_json=%s::jsonb, validation=%s::jsonb, "
                    "status=%s, review_loop=review_loop+1, updated=now() WHERE id=%s",
                    (json.dumps(std), json.dumps({"passed": ok, "flagged_fields": flags}),
                     final_status, doc_id))
        task_id = None
        if deliverable:
            task_id = str(uuid.uuid4())
            cur.execute("INSERT INTO delivery_tasks(id,document_id,target_url,status) "
                        "VALUES(%s,%s,%s,'QUEUED')", (task_id, doc_id, TARGET_API_URL))
        c.commit()
        if deliverable:
            publish("deliver", {"task_id": task_id, "document_id": doc_id})
        total_chars = sum(len(p["text"]) for p in std.get("pages", pages_text)) if std.get("kind") == "pdf" else len(std.get("ocr", ""))
        return {"document_id": doc_id, "engine": engine, "pages_ocr": len(pages_text),
                "chars": total_chars, "validated": ok, "flags": flags, "delivery_task": task_id,
                "split_parent": bool(std.get("parts_created"))}


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
        cur.execute("SELECT id,filename,mime,size,status,review_loop,created,updated,folder,rel_path "
                    "FROM documents WHERE status <> 'SPLIT_PARENT' ORDER BY created DESC LIMIT %s", (limit,))
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
        cur.execute("SELECT child_document_id,page_start,page_end,detected_class,confidence,status FROM document_parts WHERE parent_document_id=%s ORDER BY page_start", (doc_id,))
        d["parts"] = [dict(r) for r in cur.fetchall()]
    return d


@app.get("/documents/{doc_id}/progress")
def get_document_progress(doc_id: str):
    """Lightweight polling endpoint for OCR-only uploads."""
    with db() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT id,filename,status,standard_json,created,updated FROM documents WHERE id=%s", (doc_id,))
        d = cur.fetchone()
    if not d:
        raise HTTPException(404, "not found")
    std = d.get("standard_json") or {}
    progress = std.get("ocr_progress") or {}
    pages = std.get("pages") or []
    return {
        "document_id": d["id"], "filename": d["filename"], "status": d["status"],
        "ocr_progress": progress, "pages_available": len(pages),
        "raw_text_chars_available": sum(len(p.get("text", "")) for p in pages),
        "updated": d["updated"]
    }


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
        f"<td>{d['size']}</td><td>{d['created']}</td>"
        f"<td>{H.escape(str(d.get('folder') or '')[:40])}</td></tr>" for d in docs) or \
        "<tr><td colspan='6'>no documents yet</td></tr>"
    return _page("Doc Pipeline — Documents", f"""<h1>Doc Pipeline</h1>
<div class='sub'>Ingested documents, newest first · <a href='/view/upload'>upload folder</a> · <a href='/view/inbox'>receiving inbox</a> · <a href='/view/settings'>OCR settings</a> · <a href='/docs'>API console</a></div>
<table><tr><th>ID</th><th>File</th><th>Status</th><th>Bytes</th><th>Created</th><th>Folder</th></tr>{rows}</table>""")


UPLOAD_HTML = """<!doctype html><html><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Upload folder — Doc Pipeline</title>
<style>body{font-family:system-ui,sans-serif;background:#f4f5f7;margin:0;padding:24px;color:#1a1a2e}
.card{max-width:820px;margin:auto;background:#fff;border-radius:12px;padding:24px;box-shadow:0 2px 8px #0001}
label{display:block;font-weight:600;margin:14px 0 4px}input,select{width:100%;padding:9px;border:1px solid #ccc;border-radius:8px;box-sizing:border-box;font-size:14px}
button{margin-top:16px;padding:10px 18px;border:0;border-radius:8px;background:#2563eb;color:#fff;font-size:14px;cursor:pointer}
button.sec{background:#e5e7eb;color:#111}button:disabled{opacity:.5;cursor:not-allowed}
#out{margin-top:14px;padding:10px;border-radius:8px;font-size:13px;white-space:pre-wrap;display:none}
.ok{background:#e7f7ec;color:#166534;display:block}.err{background:#fde8e8;color:#991b1b;display:block}
.mut{background:#eef2ff;color:#3730a3;display:block}.hint{font-size:12px;color:#666;margin-top:3px}
ul{font-size:12px;color:#444;max-height:180px;overflow:auto;background:#f8fafc;padding:8px 8px 8px 24px;border-radius:8px}
.bar{height:10px;background:#e5e7eb;border-radius:6px;margin-top:8px;overflow:hidden}
.bar>i{display:block;height:100%;background:#2563eb;width:0%;transition:width .4s}
.nav{font-size:13px;margin-bottom:14px}</style></head><body>
<div class='nav'><a href='/view'>← documents</a></div>
<div class='card'><h2>Upload a folder — DFS finds every PDF</h2>
<div class='hint'>Pick any folder (subfolders included — depth-first). PDF, JPG, PNG, WEBP, XLSX are ingested; duplicates by sha256 are skipped; OCR runs automatically per file (4 workers).</div>
<label>Folder <span class='hint'>(whole tree)</span></label>
<input type="file" id="dir" webkitdirectory directory multiple>
<label>Or: individual files</label>
<input type="file" id="files" multiple>
<label>Doc type hint <span class='hint'>(optional; auto-classify when empty)</span></label>
<input id="dtype" placeholder="other">
<button id="go" onclick="send()">Upload & start batch OCR</button> <button class="sec" onclick="poll()" id="pb" disabled>Show live progress</button>
<div id="pv" class="mut" style="display:none"></div>
<div id="out"></div>
<div class="bar" id="barw" style="display:none"><i id="bar"></i></div>
<pre id="log" style="background:#0f172a;color:#d7e2f2;padding:10px;border-radius:8px;font-size:11px;max-height:220px;overflow:auto;display:none"></pre>
</div><script>
const $=id=>document.getElementById(id);
let picked=[];
function fmt(b){return b>1048576?(b/1048576).toFixed(1)+' MB':(b/1024).toFixed(0)+' KB'}
$('dir').onchange=e=>{picked=[...e.target.files];show()};
$('files').onchange=e=>{picked=[...e.target.files];show()};
function show(){
  const t=picked.reduce((a,f)=>a+f.size,0);
  $('pv').style.display='block';
  $('pv').innerHTML=`<b>${picked.length} files</b> · ${fmt(t)} — DFS will upload all of them`+
    `<ul>${picked.slice(0,50).map(f=>`<li>${f.webkitRelativePath||f.name} (${fmt(f.size)})</li>`).join('')}${picked.length>50?`<li>… ${picked.length-50} more</li>`:''}</ul>`;
  $('go').disabled=!picked.length;
}
async function send(){
  if(!picked.length)return say('err','pick a folder first');
  $('go').disabled=true;say('mut','uploading '+picked.length+' files…');
  const fd=new FormData();
  for(const f of picked)fd.append('files',f,f.webkitRelativePath||f.name);
  fd.append('folder_name',(picked[0].webkitRelativePath||'').split('/')[0]||'browser');
  fd.append('doc_type',$('dtype').value||'other');
  try{
    const r=await fetch('/documents/folder',{method:'POST',body:fd});
    const d=await r.json();
    if(!r.ok)throw new Error(d.detail||r.status);
    say('ok','accepted: '+d.total+' files queued → DFS walk + OCR started in background');
    $('pb').disabled=false;poll();
  }catch(e){say('err',String(e.message||e));$('go').disabled=false;}
}
let tm=null;
async function poll(){
  clearInterval(tm);tm=setInterval(show1,3000);show1();
}
async function show1(){
  try{
    const s=await(await fetch('/documents/folder/status')).json();
    $('barw').style.display='block';
    $('bar').style.width=(s.total?Math.round(100*s.processed/s.total):0)+'%';
    $('log').style.display='block';
    $('log').textContent=(s.running?'RUNNING ':'')+s.folder+' — '+s.processed+'/'+s.total+
      ' · '+JSON.stringify(s.counts)+'\\n'+(s.log||[]).join('\\n');
    if(!s.running&&s.finished){clearInterval(tm);$('go').disabled=false;
      say('ok','DONE '+s.folder+' · '+JSON.stringify(s.counts));}
  }catch(e){}
}
function say(cls,msg){const o=$('out');o.className=cls;o.textContent=msg;o.style.display='block';}
</script></body></html>"""


@app.get("/view/upload", response_class=HTMLResponse)
def view_upload():
    return _page("Upload folder", UPLOAD_HTML)


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
<div class='card'>
<h2 style='margin:0 0 6px'>Mapping AI (engine api)</h2>
<p style='font-size:13px;color:#555;margin:0 0 8px'>Same Token Plan key style as OCR. Rules digest from <code>/knowledge/po_rules.json</code> + <code>issuers.json</code> is injected automatically. Engine <code>hermes</code> = deterministic parsers (default, zero API cost); <code>api</code> = LLM maps via /mapping/run.</p>
<label>Engine
<select id='m_engine'><option value='hermes'>hermes (parsers, free)</option><option value='api'>api (LLM token plan)</option><option value='hybrid'>hybrid (parsers first, LLM for REVIEW pages)</option></select></label>
<label>Model (api)</label><input id='m_model' placeholder='qwen3.8-flash'>
<label>Endpoint (api) — blank = reuse OCR endpoint</label><input id='m_endpoint' placeholder='https://...compatible-mode/v1'>
<label>API key (api) — blank = keep saved</label><input id='m_key' type='password' placeholder='(saved)'>
<label>Auto-map after OCR?
<select id='m_auto'><option value='0'>no — queue only, I trigger</option><option value='1'>yes — enqueue+mapped automatically</option></select></label>
<button onclick='saveMap()'>Save mapping settings</button> <button onclick='testMap()'>Test on 1 pending doc</button>
<pre id='mout' style='white-space:pre-wrap;font-size:12px;background:#0b1020;color:#9fe870;padding:10px;border-radius:8px;display:none'></pre>
</div>
<script>
async function loadMap(){const c=await(await fetch('/settings/map')).json();
 m_engine.value=c.engine||'hermes'; m_model.value=c.model||''; m_endpoint.value=(c.endpoint&&c.endpoint!==c.ocr_endpoint)?c.endpoint:(c.endpoint||'');
 m_key.placeholder=c.key||'(empty)'; m_auto.value=c.auto||'0';}
function mbody(){const b={};if(m_engine.value)b.engine=m_engine.value;if(m_model.value)b.model=m_model.value;
 if(m_endpoint.value)b.endpoint=m_endpoint.value;if(m_key.value)b.key=m_key.value;b.auto=m_auto.value;return b;}
async function saveMap(){const o=document.getElementById('mout');o.style.display='block';o.textContent='saving...';
 const r=await fetch('/settings/map',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(mbody())});
 o.textContent=JSON.stringify(await r.json(),null,1);loadMap();}
async function testMap(){const o=document.getElementById('mout');o.style.display='block';o.textContent='testing (reads 1 pending doc, no DB write)...';
 const b=mbody();b.key=m_key.value||b.key;
 const r=await fetch('/settings/map/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});
 let j;try{j=await r.json()}catch(e){o.textContent='HTTP '+r.status;return}
 o.textContent=JSON.stringify(j,null,1);}
loadMap();
</script>load();
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
