import subprocess, json, os, shutil, hashlib, time, re

BASE = '/home/suryahalim/doc-pipeline'
SNAP = os.path.join(BASE, 'snapshots')
ts = '2026-09-30'

# 1) copy latest state into snapshots (tracked, scrubbed)
copies = {
    '/tmp/sheet_now.json':      f'sheet_live_{ts}.json',
    '/tmp/dump_restore.json':   f'dump_final_{ts}.json',
    '/tmp/dump_new2.json':      f'dump_schema12_{ts}.json',
    'knowledge/overrides.json': 'overrides_latest.json',
    'knowledge/po_formats.json': 'po_formats_latest.json',
    'knowledge/dv_queue.json':  'dv_queue_latest.json',
    'knowledge/dv_run.log':     'dv_run_latest.log',
}
os.chdir(BASE)
scrub = re.compile(rb'(refresh_?token|client_?secret|api_?key|token_uri|sk-|ya29\.)', re.I)
for src, dst in copies.items():
    if not os.path.exists(src):
        print('skip missing', src); continue
    data = open(src, 'rb').read()
    hits = set(m.group(0) for m in scrub.finditer(data))
    if hits:
        print('SCRUB-HITS in', src, hits)
        for h in hits:
            data = data.replace(h, b'[scrubbed-]')
    open(os.path.join(SNAP, dst), 'wb').write(data)
    print('snap', dst, len(data), 'bytes')

# 2) tidy helper scripts into scripts/diagnostics
diag = os.path.join(BASE, 'scripts', 'diagnostics')
os.makedirs(diag, exist_ok=True)
for f in os.listdir(os.path.join(BASE, 'scripts')):
    if f.startswith('_') and f.endswith('.py'):
        shutil.copy(os.path.join(BASE, 'scripts', f), os.path.join(diag, f))
print('diagnostics:', len(os.listdir(diag)), 'files')

# 3) local full tar (not pushed to github; heavy)
tar = f'/home/suryahalim/backup_doc-pipeline_{ts}.tar.gz'
r = subprocess.run(['tar', 'czf', tar, '--exclude=backups', '--exclude=data',
                    '--exclude=__pycache__', '-C', '/home/suryahalim', 'doc-pipeline'],
                   capture_output=True, text=True)
print('tar rc', r.returncode, os.path.getsize(tar), 'bytes', r.stderr[:200])

# 4) git add/commit/push doc-pipeline
r = subprocess.run(['git', 'add', '-A'], capture_output=True, text=True)
print('git add rc', r.returncode, r.stderr[:200])
msg = ('backup: full state 2026-09-30 - restore from external-tab damage (rev291/307), '
       'PO schema12 final dumps, live sheet snapshot, deep-verify knowledge (overrides 1864, '
       'po_formats registry, dv queue), diagnostic scripts kept in scripts/diagnostics')
r = subprocess.run(['git', 'commit', '-q', '-m', msg], capture_output=True, text=True)
print('commit rc', r.returncode, (r.stdout or r.stderr)[:200])
r = subprocess.run(['git', 'push', '-q', 'origin', 'main'], capture_output=True, text=True)
print('push rc', r.returncode, (r.stderr or '')[:300])
r = subprocess.run(['git', 'log', '--oneline', '-1'], capture_output=True, text=True)
print('HEAD', r.stdout.strip())
r = subprocess.run(['git', 'status', '--short'], capture_output=True, text=True)
print('remaining dirty:', r.stdout[:200] or '(clean)')
