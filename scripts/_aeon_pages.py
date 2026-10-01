import json
c = json.load(open('/tmp/batch_map_docs.json'))
for did, d in c.items():
    if '7000363700' in str(d.get('filename', '')):
        for p in (d.get('standard_json') or {}).get('pages', []):
            if p.get('page') in (6, 7, 15):
                print('=' * 30, 'PAGE', p['page'])
                print((p.get('text') or '')[:1500])
        break
