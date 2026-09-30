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
print('spreadsheetId in copy_sid.txt =', sid)

meta = svc.spreadsheets().get(spreadsheetId=sid,
    fields='properties.title,sheets.properties').execute()
print('TITLE:', meta['properties']['title'])
for s in meta['sheets']:
    p = s['properties']
    print(f"  gid={p['sheetId']:>12}  {p['title']}")

r = svc.spreadsheets().values().get(spreadsheetId=sid, range='PO Customer!A1:L6').execute()
vals = r.get('values', [])
print('\nPO Customer first rows:')
for row in vals:
    print('  ', row)
