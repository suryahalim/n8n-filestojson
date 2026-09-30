import json
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build as apibuild

d = json.load(open('/home/suryahalim/.hermes/google_token.json'))
creds = Credentials(
    token=d.get('token'),
    client_id=d['client_id'],
    client_secret=d['client_secret'],
    refresh_token=d['refresh_token'],
    token_uri=d.get('token_uri', 'https://oauth2.googleapis.com/token'),
    scopes=d.get('scopes'),
)
svc = apibuild('sheets', 'v4', credentials=creds)
sid = open('/home/suryahalim/doc-pipeline/copy_sid.txt').read().strip()
meta = svc.spreadsheets().get(spreadsheetId=sid, fields='sheets.properties.title').execute()
print('=== CURRENT LIVE STATE ===')
for s in meta['sheets']:
    t = s['properties']['title']
    r = svc.spreadsheets().values().get(spreadsheetId=sid, range=t + '!A1:Z3').execute()
    v = r.get('values', [])
    cols = len(v[0]) if v else 0
    # count total non-empty data rows via full read
    rf = svc.spreadsheets().values().get(spreadsheetId=sid, range=t).execute()
    data = rf.get('values', [])
    nonempty = sum(1 for row in data[1:] if any((c or '').strip() for c in row))
    print(f'{t:22} headercols={cols:2} datarows={nonempty:5}')
    if v:
        print('    header:', [str(c)[:22] for c in v[0]])
