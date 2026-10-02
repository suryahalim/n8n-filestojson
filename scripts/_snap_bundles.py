import json, sys
sys.path.insert(0, '/home/suryahalim/doc-pipeline/scripts')
import psycopg2, os, re
env = dict(l.split('=', 1) for l in open('/home/suryahalim/doc-pipeline/.env') if '=' in l)
conn = psycopg2.connect(host='100.68.212.36', port=5433, user='pipeline',
                        password=env.get('PIPELINE_DB_PASSWORD', '').strip(), dbname='pipeline')
cur = conn.cursor()
cur.execute("SELECT id, filename, folder, rel_path, status, standard_json FROM documents WHERE folder LIKE 'Complete bundles%' ORDER BY folder, rel_path")
out = {}
for did, fn, folder, rel, status, std in cur.fetchall():
    std = std if isinstance(std, dict) else json.loads(std or '{}')
    pages = std.get('pages') or [{'page': 1, 'text': std.get('extracted', {}).get('ocr', '')}]
    out[did] = {'filename': fn, 'folder': folder, 'rel_path': rel, 'status': status, 'pages': pages}
json.dump(out, open('/home/suryahalim/doc-pipeline/snapshots/bundle_docs_ocr_2026-10-02.json', 'w'))
n = sum(len(v['pages']) for v in out.values())
print('docs:', len(out), '| pages:', n)
