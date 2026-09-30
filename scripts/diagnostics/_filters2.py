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

meta = svc.spreadsheets().get(spreadsheetId=sid).execute()
for s in meta['sheets']:
    p = s['properties']; t = p['title']
    gid = p['sheetId']
    gf = s.get('gridProperties', {})
    fh = gf.get('frozenRowCount'), gf.get('frozenColumnCount')
    rows = s.get('data', [])
    # detect hidden rows via includeGridData not available here; check basicFilter/si
    basic = s.get('basicFilter')
    print(f'{t:20} gid={gid} frozen={fh} basicFilter={bool(basic)}',
          (json.dumps(basic)[:120] if basic else ''))
