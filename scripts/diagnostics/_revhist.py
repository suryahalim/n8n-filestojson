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
drv = apibuild('drive', 'v3', credentials=creds)
sid = open('/home/suryahalim/doc-pipeline/copy_sid.txt').read().strip()
res = drv.revisions().list(fileId=sid, pageSize=12,
        fields='revisions(id,modifiedTime,lastModifyingUser(displayName,emailAddress),kind)').execute()
for r in res.get('revisions', []):
    u = (r.get('lastModifyingUser') or {}).get('displayName', '?')
    print(r['modifiedTime'], '|', u, '| rev', r['id'])
