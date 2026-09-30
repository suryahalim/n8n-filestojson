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

meta = svc.spreadsheets().get(spreadsheetId=sid,
    fields='sheets.properties,sheets.dataFilters,sheets.hiddenGridProperties,sheets.columnGroups,sheets.rowGroups').execute()
for s in meta['sheets']:
    p = s['properties']
    t = p['title']
    hid = (s.get('hiddenGridProperties') or {}).get('rowHidden', [])
    nhidden = sum(1 for h in hid if h)
    filters = s.get('dataFilters', [])
    print(f'{t:20} index={p.get("index")} hiddenRows={nhidden} filters={len(filters)}')
    for fl in filters[:3]:
        print('    FILTER:', json.dumps(fl)[:160])
# also basic filter on sheets API 'bandedRange'? sheet-level basicFilter exists for old filter
meta2 = svc.spreadsheets().get(spreadsheetId=sid, fields='sheets.properties.basicFilter').execute()
for s in meta2['sheets']:
    if s.get('basicFilter'):
        print('BASIC FILTER on', s['properties']['title'], json.dumps(s['basicFilter'])[:200])
