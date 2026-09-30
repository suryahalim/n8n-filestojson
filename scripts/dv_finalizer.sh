#!/bin/bash
# finalizer: wait for deep_verify loop to converge, then rewrite sheets + verify.
cd /home/suryahalim/doc-pipeline
SID=$(cat copy_sid.txt)
while true; do
  if ! pgrep -f 'deep_verify.py --loop' >/dev/null; then
    echo "loop ended at $(date)"
    break
  fi
  sleep 60
done
# one more free P0 sweep in case late fixes unblock pages
python3 -u scripts/deep_verify.py --p0
echo "=== rewriting sheets with all overrides ==="
python3 -u scripts/batch_map.py --dump /tmp/dump_dv.json \
  --window '2026-09-24T12:00|2026-09-24T12:20|efaktur' \
  --window '2026-09-24T13:00|2026-09-26T23:59|scans' "$SID" || { echo 'FINAL WRITE FAILED'; exit 1; }
python3 -u scripts/read_sheet.py || { echo 'READ BACK FAILED'; exit 1; }
echo FINAL WRITE DONE