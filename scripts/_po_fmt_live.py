import json, requests
tok = json.load(open('/home/suryahalim/.hermes/google_token.json'))
s = requests.Session()
s.headers['Authorization'] = f"Bearer {tok['token']}"
sid = open('/home/suryahalim/doc-pipeline/copy_sid.txt').read().strip()
API = 'https://sheets.googleapis.com/v4/spreadsheets/' + sid
r = s.get(API + '/values/PO Customer!A1:M12').json()
for row in r.get('values', []):
    print(' | '.join(str(c)[:22] for c in row))
print()
r2 = s.get(API + '/values/PO Customer!A600:M606').json()
for row in r2.get('values', []):
    print(' | '.join(str(c)[:22] for c in row))
