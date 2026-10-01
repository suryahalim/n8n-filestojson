import json, subprocess
sid = open('/home/suryahalim/doc-pipeline/copy_sid.txt').read().strip()
d = json.load(open('/tmp/dump_v2d.json'))
sheets = d['sheets']
review = d.get('review')
import sys
sys.path.insert(0, '/home/suryahalim/doc-pipeline/scripts')
from map_sheets import write, HEADERS
from batch_map import FP_HEADERS
HEADERS['Faktur Pajak'] = FP_HEADERS
res = write(sid, dict(sheets), review)
print('written:', json.dumps(res, ensure_ascii=False))
# read-back verify
r = subprocess.run(['python3', 'scripts/read_sheet.py'], capture_output=True, text=True,
                   cwd='/home/suryahalim/doc-pipeline', timeout=600)
sh = json.load(open('/tmp/sheet_now.json'))
po = sh['PO Customer']
hdr = po[0]
data = po[1:]
print('live header:', hdr)
print('live PO rows:', len(data))
print('col count:', len(hdr))
import collections
print('status:', collections.Counter(str(x[12]).split('|')[0] for x in data).most_common(8))
print('issuer filled:', sum(1 for x in data if str(x[2]).strip()))
print('vcode filled:', sum(1 for x in data if str(x[1]).strip()))
print('ppn:', collections.Counter(str(x[3]) for x in data if str(x[3]).strip()))
