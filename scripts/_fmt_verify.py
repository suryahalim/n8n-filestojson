import json, requests
tok = json.load(open('/home/suryahalim/.hermes/google_token.json'))
s = requests.Session()
s.headers['Authorization'] = f"Bearer {tok['token']}"
sid = open('/home/suryahalim/doc-pipeline/copy_sid.txt').read().strip()
r = s.get(f'https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/PO Customer!A2:M4',
          params={'valueRenderOption': 'FORMATTED_VALUE'}).json()
for row in r.get('values', []):
    print(' | '.join(str(c)[:24] for c in row))
g = s.get(f'https://sheets.googleapis.com/v4/spreadsheets/{sid}',
          params={'ranges': ['PO Customer!A1:M3'], 'includeGridData': 'true',
                  'fields': 'sheets.data.gridData.rowData.values.formattedValue,sheets.data.gridData.rowData.values.effectiveFormat.numberFormat'}).json()
vals = g['sheets'][0]['data'][0]['gridData']['rowData']
for rd in vals[:2]:
    fmts = []
    for c in rd['values'][:13]:
        nf = (c.get('effectiveFormat') or {}).get('numberFormat') or {}
        fmts.append(f"{nf.get('type','?')}/{nf.get('pattern','')}")
    print('fmt:', fmts)
