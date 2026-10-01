import json, re, sys
sys.path.insert(0, '/home/suryahalim/doc-pipeline/scripts')
import map_sheets as MS

corpus = json.load(open('/tmp/batch_map_docs.json'))
d = corpus['058311d6333d4f5cb00d245c6a215ae5']
pgs = {p['page']: (p.get('text') or '') for p in (d.get('standard_json') or {}).get('pages', [])}

for n in (5, 111, 118, 121, 143, 153, 104, 161):
    t = pgs.get(n) or ''
    cat = MS.classify(t)
    u = t.upper()
    # replicate build() classifier chain
    try:
        po = MS.po_primafood_pipe(t, n) or MS.po_farmers_horizontal(t, n) or MS.po_dfj_inline(t, n) \
             or MS.po_dfj_horizontal(t, n) or MS.po_tiptop_po(t, n) or MS.po_boots_inline(t, n) \
             or MS.po_aeon(t, n) or MS.po_dfj_vertical(t, n) or MS.po_boots_vertical(t, n) \
             or MS.po_tiptop(t, n) or MS.po_mitra(t, n) or MS.po_primafood(t, n) or MS.po_yogya(t, n) \
             or MS.po_kalimalang(t, n)
    except Exception as e:
        po = f'EXC {e!r}'
    print(f'p{n:>3} cat={cat:15s} rows={len(po) if isinstance(po,list) else po}')
    if n == 5:
        # which pattern matches?
        for name in ['po_primafood_pipe','po_farmers_horizontal','po_dfj_inline','po_dfj_horizontal',
                     'po_tiptop_po','po_boots_inline','po_aeon','po_dfj_vertical','po_boots_vertical',
                     'po_tiptop','po_mitra','po_primafood','po_yogya','po_kalimalang']:
            f = getattr(MS, name)
            try:
                r = f(t, n)
            except Exception as e:
                r = f'EXC {e!r}'
            if r:
                print('   MATCH:', name, len(r) if isinstance(r, list) else r)
                if isinstance(r, list) and r:
                    print('   sample row:', [str(c)[:30] for c in r[0]])
print('--- PO label greps on p5 ---')
t5 = pgs[5]
print('PO#:', re.findall(r'PO#\s*:\s*([^\n]+)', t5))
print('VENDOR:', re.findall(r'VENDOR\s*:\s*([^\n]+)', t5))
print('classify regexes: ^PURCHASE ORDER$ line?', bool(re.search(r'^(?:---)?PURCHASE ORDER(?:---)?\s*$', t5.upper(), re.M)))
