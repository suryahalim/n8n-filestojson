import json, re, sys, collections
sys.path.insert(0, '/home/suryahalim/doc-pipeline/scripts')
import knowledge as KB

corpus = json.load(open('/tmp/batch_map_docs.json'))
did = '058311d6333d4f5cb00d245c6a215ae5'
d = corpus[did]
FN = d['filename']
pgs = {p['page']: (p.get('text') or '') for p in (d.get('standard_json') or {}).get('pages', [])}

sh = json.load(open('/tmp/sheet_now.json'))
# sheet rows carry 'pNNN <filename>' in Source Page; collect per page which tab
IN = {'Faktur Penjualan': 15, 'PO Customer': 10, 'Tanda Terima': 8,
      'Surat Jalan': 8, 'Dokumen Pelunasan': 9, 'OCR Mapping Review': 4}
landed = collections.defaultdict(list)
for tab, idx in IN.items():
    if tab not in sh:
        continue
    for r in sh[tab][1:]:
        sp = str(r[idx]) if len(r) > idx else ''
        m = re.match(r'p(\d+)\s+7000362345 - 7000362399', sp)
        if m:
            landed[int(m.group(1))].append(tab)

def kind(txt):
    if re.search(r'PURCHASE ORDER', txt, re.I):
        return 'PO'
    if re.search(r'PROFORMA INVOICE', txt, re.I):
        return 'PROFORMA'
    if re.search(r'FAKTUR PENJUALAN|Sales Order \[SO\]', txt, re.I):
        return 'FP'
    if re.search(r'TANDA TERIMA', txt, re.I):
        return 'TT'
    if re.search(r'SURAT JALAN', txt, re.I):
        return 'SJ'
    return 'other'

cens = collections.Counter()
for p in sorted(pgs):
    k = kind(pgs[p])
    tabs = landed.get(p, [])
    cens[(k, bool(tabs))] += 1
    if not tabs:
        print(f'MISSING p{p:>3} kind={k:9s} first={pgs[p].splitlines()[0][:50]!r} len={len(pgs[p])}')
print('=== census (kind, landed?) ===', dict(cens))
