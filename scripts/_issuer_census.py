import json, re, collections, sys
sys.path.insert(0, '/home/suryahalim/doc-pipeline/scripts')

corpus = json.load(open('/tmp/batch_map_docs.json'))

# 1) census: pages across ENTIRE corpus that carry PO markers, by issuer label shape
issuers = collections.Counter()
po_pages = 0
for did, d in corpus.items():
    for p in (d.get('standard_json') or {}).get('pages', []):
        t = p.get('text') or ''
        u = t.upper()
        if 'PURCHASE ORDER' not in u:
            continue
        po_pages += 1
        m = re.search(r'PT\.?\s*([A-Z][A-Za-z .,&\'-]{3,40}?(?:TBK|Tbk|, Tbk|CO)|MART|INDO|GROUP)', t, re.I)
        if m:
            issuers[m.group(0).strip().upper()[:32]] += 1
print('pages containing PURCHASE ORDER (all corpus):', po_pages)
print('top issuer-ish PT names:')
for k, v in issuers.most_common(25):
    print(f'  {v:4d}  {k}')

# 2) what does column B (Vendor) currently hold for parsed PO rows?
po = json.load(open('/tmp/sheet_now.json'))['PO Customer']
vb = collections.Counter(str(r[1]) for r in po[1:] if len(r) > 11 and r[11] != 'OCR_PAGE_REVIEW')
print('\ncurrent col-B values (top 15):')
for k, v in vb.most_common(15):
    print(f'  {v:4d}  {k[:48]}')
