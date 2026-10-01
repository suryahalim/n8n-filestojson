import json
c = json.load(open('/tmp/batch_map_docs.json'))
wins = [('2026-09-24T12:00', '2026-09-24T12:20', 'efaktur'),
        ('2026-09-24T13:00', '2026-09-26T23:59', 'scans')]
sel = {}
for did, d in c.items():
    cr = d.get('created') or ''
    for s, u, tag in wins:
        if (not s or cr >= s) and (not u or cr < u):
            sel[did] = tag
print('docs selected by windows:', len(sel), 'of', len(c))
print('target in selection:', '058311d6333d4f5cb00d245c6a215ae5' in sel)
miss = [(did, d.get('filename'), d.get('created')) for did, d in c.items() if did not in sel]
for m in miss[:8]:
    print('NOT-SELECTED:', m)
d = c['058311d6333d4f5cb00d245c6a215ae5']
print('target created:', d.get('created'))
