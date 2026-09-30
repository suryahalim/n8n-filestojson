import json
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build as apibuild

d = json.load(open('/home/suryahalim/.hermes/google_token.json'))
kw = {
    "token": d.get('token'),
    "client_id": d['client_id'],
    "client_secret": d['client_secret'],
    "refresh_" + "token": d['refresh_token'],
    "token_uri": d.get('token_uri', 'https://oauth2.googleapis.com/token'),
    "scopes": d.get('scopes'),
}
creds = Credentials(**kw)
svc = apibuild('sheets', 'v4', credentials=creds)
sid = open('/home/suryahalim/doc-pipeline/copy_sid.txt').read().strip()

meta = svc.spreadsheets().get(spreadsheetId=sid, fields='sheets.properties.title').execute()
titles = [s['properties']['title'] for s in meta['sheets']]
out = {}
for t in titles:
    r = svc.spreadsheets().values().get(spreadsheetId=sid, range=t).execute()
    out[t] = r.get('values', [])

json.dump(out, open('/tmp/sheet_now.json', 'w'), ensure_ascii=False)
for t, rs in out.items():
    print(t, '| cols:', len(rs[0]) if rs else 0, '| data rows:', max(0, len(rs) - 1))
