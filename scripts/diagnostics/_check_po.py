import json
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build as apibuild

d = json.load(open('/home/suryahalim/.hermes/google_token.json'))
creds = Credentials(
    token=d.get('token'), client_id=d['client_id'], client_secret=d['client_secret'],
    refresh_token=d['refresh_token'], token_uri=d.get('token_uri', 'https://oauth2.googleapis.com/token'),
    scopes=d.get('scopes'))
svc = apibuild('sheets', 'v4', credentials=creds)
sid = open('/home/suryahalim/doc-pipeline/copy_sid.txt').read().strip()
out = {}
meta = svc.spreadsheets().get(spreadsheetId=sid, fields='sheets.properties.title').execute()
for s in meta['sheets']:
    t = s['properties']['title']
    r = svc.spreadsheets().values().get(spreadsheetId=sid, range=t).execute()
    out[t] = r.get('values', [])
json.dump(out, open('/tmp/sheet_now.json', 'w'), ensure_ascii=False)

po = out['PO Customer']
hdr = po[0]
data = po[1:]
print('PO rows:', len(data), '| header cols:', len(hdr))
# integrity checks vs expected schema
bad_po = [x for x in data if not (x[0] or '').strip()]
bad_code = [x for x in data if len(x) > 3 and x[3] and not str(x[3]).strip().isdigit()]
dup = {}
for x in data:
    k = (x[0], str(x[3]) if len(x) > 3 else '', str(x[10]) if len(x) > 10 else '')
    dup[k] = dup.get(k, 0) + 1
dups = {k: v for k, v in dup.items() if v > 1}
print('empty PO#:', len(bad_po), '| non-numeric code cells:', len(bad_code), '| dup rows:', len(dups))
print('sample rows:')
for x in data[:3]:
    print('  ', [str(c)[:20] for c in x])
# check for stray edits: rows whose Source Page col missing / shifted content
weird = [x for x in data if len(x) < 12 or (x[10] and not str(x[10]).strip().isdigit())]
print('short/malformed rows:', len(weird))
for x in weird[:5]:
    print('  !', [str(c)[:18] for c in x])
