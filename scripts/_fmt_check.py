import json, requests
tok = json.load(open('/home/suryahalim/.hermes/google_token.json'))
s = requests.Session()
s.headers['Authorization'] = f"Bearer {tok['token']}"
sid = open('/home/suryahalim/doc-pipeline/copy_sid.txt').read().strip()
API = 'https://sheets.googleapis.com/v4/spreadsheets/' + sid

# 1) values sample: top rows + middle
r = s.get(API, params={'ranges': ['PO Customer!A1:M16', 'PO Customer!A600:M612'],
                       'includeGridData': 'true', 'fields':
                       'sheets.data.gridData.rowData.values.userEnteredFormat.numberFormat,sheets.data.gridData.rowData.values.effectiveFormat,sheets.data.rowData.values.formattedValue,sheets.data.rowData.values.userEnteredValue,sheetId'}).json()
print(json.dumps(r, ensure_ascii=False)[:1200])
