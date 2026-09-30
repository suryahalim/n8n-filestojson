import json, re, subprocess

# pull raw OCR text of the 7000363700-7000363703 booklet page 1 to see the real table columns
cmd = ['docker', 'exec', 'dp-db', 'psql', '-U', 'pipeline', '-d', 'pipeline', '-A', '-t', '-c',
       "SELECT standard_json FROM documents WHERE filename='7000363700 - 7000363703.pdf';"]
r = subprocess.run(cmd, capture_output=True, text=True)
out = (r.stdout + r.stderr).strip()
try:
    sj = json.loads(out.replace('\n', ' ')) if out.startswith('[') else json.loads(json.loads(out))
except Exception:
    sj = json.loads(json.loads(out)) if out.startswith('"') else json.loads(out)
pages = sj['pages'] if isinstance(sj, dict) and 'pages' in sj else sj
p0 = pages[0]
text = p0['text'] if isinstance(p0, dict) else str(p0)
print('--- RAW OCR page 1 (first 1600 chars) ---')
print(text[:1600])
