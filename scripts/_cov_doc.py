import json, re, sys, collections
sys.path.insert(0, '/home/suryahalim/doc-pipeline/scripts')
import knowledge as KB

corpus = json.load(open('/tmp/batch_map_docs.json'))
FID = '7000362345 - 7000362399'
pages = KB.build_page_lookup(corpus)[FID]
NP = len(pages)
sheet = json.load(open('/tmp/sheet_now.json'))

SPIDX = {'Faktur Penjualan': 15, 'PO Customer': 10, 'Tanda Terima': 8,
         'Surat Jalan': 8, 'Faktur Pajak': 11, 'Dokumen Pelunasan': None}

# this doc's rows in sheet: Source Page encoded per-run; use text signature instead.
# 1) which pages of THIS doc carry which document type
kinds = {}
for p in sorted(pages):
    txt = '\n'.join(pages[p])
    k = '-'
    if re.search(r'PROFORMA INVOICE', txt, re.I): k = 'PROFORMA'
    elif re.search(r'PURCHASE ORDER|PO Issued|ORDER NUMBER', txt, re.I): k = 'PO'
    elif re.search(r'FAKTUR PENJUALAN', txt, re.I): k = 'FP'
    elif re.search(r'TANDA TERIMA', txt, re.I): k = 'TT'
    elif re.search(r'SURAT JALAN', txt, re.I): k = 'SJ'
    elif re.search(r'PELUNASAN|NOTA PENAGIHAN|BILLING', txt, re.I): k = 'PLN'
    kinds[p] = k
print('page-type census:', collections.Counter(kinds.values()))
print('PO pages:', [p for p, k in kinds.items() if k == 'PO'][:30])
print('PROFORMA pages:', [p for p, k in kinds.items() if k == 'PROFORMA'][:10])

# 2) does the sheet contain rows sourced from this doc? match a unique SO/PO token
shtxt = json.dumps(sheet)
probe_sos = [m.group(1) for m in re.finditer(r'Sales Order \[SO\] #: \**(SOR\d+)',
            '\n'.join(pages[p] if isinstance(pages[p], str) else '\n'.join(pages[p]) for p in sorted(pages)[:6]))]
probe_pos = [m.group(1) for m in re.finditer(r'(?:Nomor PO|PO Number|PO No\.?|PO Issued)[:\s#]*([A-Z0-9./-]{5,})',
            '\n'.join('\n'.join(pages[p]) for p in [pp for pp,k in kinds.items() if k=='PO'][:8]))]
print('sample SO probes:', probe_sos[:5])
print('sample PO probes:', probe_pos[:8])
hit_so = [s for s in probe_sos if s in shtxt]
hit_po = [s for s in probe_pos if s in shtxt]
print('SO present in sheet:', len(hit_so), '/', len(probe_sos), '| PO present:', len(hit_po), '/', len(probe_pos))
