"""Worker: consumes job.deliver from RabbitMQ (the service bus), pushes the
standard JSON envelope to the receiving API, commits the outcome:
 - HTTP 2xx -> DELIVERED (mark complete, ack)
 - error    -> increment attempts, requeue with backoff via DB due-time;
               after max_attempts -> FAILED_PERMANENT + nack (dead-letter stays in DB).
"""
import os, json, time, threading, urllib.request, urllib.error
import psycopg2, psycopg2.extras
import pika

DATABASE_URL = os.environ["DATABASE_URL"]
RABBIT_HOST = os.environ.get("RABBIT_HOST", "rabbitmq")
RABBIT_USER = os.environ.get("RABBIT_USER", "pipeline")
RABBIT_PASS = os.environ.get("RABBIT_PASS", "")
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", "3"))
BACKOFF_BASE = 5  # seconds


def db():
    return psycopg2.connect(DATABASE_URL)


def post_json(url, payload, timeout=30):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read())


def handle(task_id: str, document_id: str):
    # idempotent claim: only proceed if task is still QUEUED/RETRY_WAIT
    with db() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("UPDATE delivery_tasks SET status='IN_FLIGHT' WHERE id=%s AND status IN ('QUEUED','RETRY_WAIT') "
                    "RETURNING target_url, attempts, max_attempts", (task_id,))
        row = cur.fetchone()
        c.commit()
    if not row:
        return  # already processed/in-flight — drop duplicate
    with db() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT standard_json,filename,sha256 FROM documents WHERE id=%s", (document_id,))
        doc = cur.fetchone()
    envelope = {"schema_version": "1.0", "document_id": document_id,
                "filename": doc["filename"], "sha256": doc["sha256"],
                "task_id": task_id, "extracted": doc["standard_json"]}
    try:
        status, body = post_json(row["target_url"], envelope)
        ok = 200 <= status < 300
    except (urllib.error.HTTPError, urllib.error.URLError, Exception) as e:
        ok, err = False, str(e)[:300]
    else:
        err = None
    with db() as c, c.cursor() as cur:
        if ok:
            cur.execute("UPDATE delivery_tasks SET status='DELIVERED', delivered_at=now(), last_error=NULL WHERE id=%s", (task_id,))
            cur.execute("UPDATE documents SET status='DELIVERED', updated=now() WHERE id=%s", (document_id,))
            c.commit()
        else:
            cur.execute("UPDATE delivery_tasks SET attempts=attempts+1, last_error=%s WHERE id=%s RETURNING attempts, max_attempts",
                        (err, task_id))
            att, mx = cur.fetchone()
            if att >= mx:
                cur.execute("UPDATE delivery_tasks SET status='FAILED_PERMANENT' WHERE id=%s", (task_id,))
                cur.execute("UPDATE documents SET status='DELIVERY_FAILED', updated=now() WHERE id=%s", (document_id,))
            else:
                delay = BACKOFF_BASE * (2 ** (att - 1))
                cur.execute("UPDATE delivery_tasks SET status='RETRY_WAIT', "
                            "next_attempt_at=now() + make_interval(secs => %s) WHERE id=%s",
                            (delay, task_id))
            c.commit()


def on_message(ch, method, props, body):
    payload = json.loads(body)
    try:
        handle(payload["task_id"], payload["document_id"])
        ch.basic_ack(delivery_tag=method.delivery_tag)
    except Exception:
        import traceback
        traceback.print_exc()
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)  # DB state drives retry


def retry_sweeper():
    """Periodically republish RETRY_WAIT tasks that are due (DB = source of truth)."""
    while True:
        time.sleep(10)
        try:
            with db() as c, c.cursor() as cur:
                cur.execute("SELECT id,document_id FROM delivery_tasks "
                            "WHERE status IN ('QUEUED','RETRY_WAIT') AND next_attempt_at <= now() FOR UPDATE SKIP LOCKED")
                due = cur.fetchall()
                conn = pika.BlockingConnection(pika.ConnectionParameters(
                    host=RABBIT_HOST, credentials=pika.PlainCredentials(RABBIT_USER, RABBIT_PASS)))
                ch = conn.channel()
                ch.exchange_declare(exchange="doc_pipeline", exchange_type="direct", durable=True)
                for tid, did in due:
                    ch.basic_publish(exchange="doc_pipeline", routing_key="job.deliver",
                                     body=json.dumps({"task_id": tid, "document_id": did}),
                                     properties=pika.BasicProperties(delivery_mode=2))
                conn.close()
        except Exception as e:
            print("sweeper err:", e, flush=True)


def main():
    threading.Thread(target=retry_sweeper, daemon=True).start()
    conn = pika.BlockingConnection(pika.ConnectionParameters(
        host=RABBIT_HOST, credentials=pika.PlainCredentials(RABBIT_USER, RABBIT_PASS),
        heartbeat=600))
    ch = conn.channel()
    ch.exchange_declare(exchange="doc_pipeline", exchange_type="direct", durable=True)
    ch.queue_declare(queue="job.deliver", durable=True)
    ch.queue_bind(queue="job.deliver", exchange="doc_pipeline", routing_key="job.deliver")
    ch.basic_qos(prefetch_count=5)
    ch.basic_consume(queue="job.deliver", on_message_callback=on_message)
    print("worker consuming job.deliver", flush=True)
    ch.start_consuming()


if __name__ == "__main__":
    main()
