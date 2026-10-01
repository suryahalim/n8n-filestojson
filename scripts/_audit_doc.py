import json, re, sys, collections
sys.path.insert(0, '/home/suryahalim/doc-pipeline/scripts')
import knowledge as KB

corpus = json.load(open('/tmp/batch_map_docs.json'))
FID = '7000362345 - 7000362399'
pages = KB.build_page_lookup(corpus).get(FID) or {}
sheet = json.load(open('/tmp/sheet_now.json'))

POSP = 7000362345  # first source page value for page 1

tabs = {}
for tab, rows in sheet.items():
    if tab in ('Dokumen',):
        continue
    idx = {'Faktur Penjualan': 15, 'PO Customer': 10, 'Tanda Terima': 8,
           'Surat Jalan': 8, 'Faktur Pajak': 11,
           'OCR Mapping Review': 4}.get(tab)
    if idx is None:
        continue
    for r in rows[1:]:
        try:
            sp = int(str(r[idx]).split('.')[0])
        except (ValueError, IndexError):
            continue
        if POSP <= sp <= POSP + 54:
            tabs.setdefault(sp - POSP + 1, []).append(tab)

print('=== per page (1-55): what landed in sheet ===')
missing, review = [], []
for p in range(1, 56):
    t = tabs.get(p, [])
    txt = (pages.get(p) or {}).get('text') or ''
    sig = []
    if re.search(r'PURCHASE ORDER|PO Issued|PO No|Nomor PO|PO Number|ORDER NUMBER', txt, re.I):
        sig.append('PO?')
    if re.search(r'FAKTUR PENJUALAN|Sales Order \[SO\]', txt, re.I):
        sig.append('FP?')
    if re.search(r'TANDA TERIMA', txt, re.I):
        sig.append('TT?')
    if re.search(r'SURAT JALAN', txt, re.I):
        sig.append('SJ?')
    pr = 'REVIEW' if 'OCR_PAGE_REVIEW' in t else ''
    if not t:
        missing.append(p)
    print(f"p{p:>2} | tabs={','.join(t) if t else 'NONE':45s} | ocr-sig={','.join(sig) or '-':20s} len={len(txt)} {pr}")
print('missing pages:', missing)
