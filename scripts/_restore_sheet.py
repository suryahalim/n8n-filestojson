import subprocess, json
sid = open('/home/suryahalim/doc-pipeline/copy_sid.txt').read().strip()
r = subprocess.run(['python3', 'scripts/batch_map.py', '--dump', '/tmp/dump_restore.json',
  '--window', '2026-09-24T12:00|2026-09-24T12:20|efaktur',
  '--window', '2026-09-24T13:00|2026-09-26T23:59|scans', sid],
  capture_output=True, text=True, cwd='/home/suryahalim/doc-pipeline', timeout=1500)
tail = [l for l in r.stdout.splitlines() if l.strip()][-10:]
print('RC', r.returncode)
for l in tail:
    print('  ', l[:180])
print('STDERR:', r.stderr[-300:])
