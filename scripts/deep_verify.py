#!/usr/bin/env python3
"""deep_verify.py — loop-until-converged verification engine for REVIEW rows.

Design (2026-09-30): never trust a single API call. Every unresolved row is
re-verified through escalating evidence stages; a row is ACCEPTED only when a
deterministic arithmetic gate proves it. Unlimited time/tokens is fine —
accuracy is the only currency. Resumable: queue+results persist in knowledge/.

Stages (cheapest first):
  P0  zero-API page corroboration: Σitem(jumlah) on the page == printed DPP
      footer of that page -> every number corroborated (PASS-DEEP/P0-SUM).
  P1  targeted vision re-read of the item table (strict JSON, 400 dpi, stored
      original). Accept whole page when substituted readings satisfy the SAME
      footer proof as P0, or per-row when a promoted KB semantics fits exactly.
  P2  second independent read (rephrased prompt); fields must AGREE across both
      reads and the arithmetic gate must pass. Disagreement -> stays REVIEW.
  P3  (planned) cross-document: booklet footer vs e-Faktur tab for same SOR.

Nothing is ever written without a proof; unreadable stays REVIEW for humans.
Outputs: knowledge/overrides.json (applied by batch_map.py before sheet write),
knowledge/dv_queue.json (checkpoint). Run:
  python3 scripts/deep_verify.py --p0
  python3 scripts/deep_verify.py --once 20
  python3 scripts/deep_verify.py --loop          # until CONVERGED
"""
import json, os, re, sys, time, argparse, collections, urllib.request

sys.path.insert(0, os.path.expanduser('~/doc-pipeline/scripts'))
import knowledge as KB

DUMP = '/tmp/dump_new2.json'  # 12-col PO schema run (2026-09-30)
CORPUS = '/tmp/batch_map_docs.json'
OVR_P = os.path.join(KB.KB_DIR, 'overrides.json')
QUE_P = os.path.join(KB.KB_DIR, 'dv_queue.json')
BASE = os.environ.get('EXTRACTOR_URL', 'http://100.68.212.36:5000')
FP = KB.FP_COLS
UNRES = ('REVIEW-ARITH', 'REVIEW')

def load(p, d):
    if os.path.exists(p):
        try:
            return json.load(open(p))
        except Exception:
            pass
    return d

def save(p, o):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump(o, open(p, 'w'), indent=1, ensure_ascii=False)

def rowkey(r):
    return f"FP|{r[FP['src']]}|{r[0]}|{(r[3] or '')[:24]}"

def n2(s):
    return KB.n2(s if isinstance(s, str) else str(s or ''))

def sem_fit(qty, harga, jumlah, sem):
    q = (qty or '').replace(' ', '')
    mq = KB.QTY_RE.match(q)
    h, j = n2(harga), n2(jumlah)
    if not (mq and h and j):
        return False
    A, B = int(mq.group(1)), int(mq.group(2))
    return A, B, h, j

def row_fits(r, qty, harga, jumlah, sem, isi):
    q = (qty or '').replace(' ', '')
    mq = KB.QTY_RE.match(q)
    h, j = n2(harga), n2(jumlah)
    if not (mq and h and j):
        return False
    A, B = int(mq.group(1)), int(mq.group(2))
    exp = {'qty=carton*isi+pcs': (A * isi + B) * h if isi else None,
           'qty=carton*isi': A * isi * h if isi else None,
           'qty=first-number': A * h}.get(sem)
    return bool(exp) and abs(exp - j) <= max(3, j * 0.002)

# ---------- P0 ----------
def p0(fp):
    ovr = load(OVR_P, {})
    pages = collections.defaultdict(lambda: dict(items=[], foot=None))
    for r in fp:
        src = r[FP['src']]
        if not src:
            continue
        g = pages[(src, r[1])]
        if r[FP['stat']] == 'SUMMARY':
            f = n2(r[12]) or n2(r[14])
            if f:
                g['foot'] = f
        else:
            g['items'].append(r)
    conv = 0
    for (src, sor), g in pages.items():
        rev = [r for r in g['items'] if r[FP['stat']] in UNRES and rowkey(r) not in ovr]
        if not g['foot'] or not rev:
            continue
        if any(n2(r[FP['jumlah']]) is None for r in g['items']):
            continue
        s = sum(n2(r[FP['jumlah']]) for r in g['items'])
        if abs(s - g['foot']) <= max(5, g['foot'] * 0.002):
            for r in rev:
                ovr[rowkey(r)] = dict(fields={}, stage='P0-SUM',
                                      evidence=f'{sor} Σitem={s:,.0f} == printed DPP={g["foot"]:,.0f}',
                                      at=time.strftime('%F %T'))
                conv += 1
    save(OVR_P, ovr)
    return conv

# ---------- vision ----------
ITEM_PROMPT = ('Look ONLY at the item table rows on this page (skip letterhead/footer). '
               'Return STRICT JSON: {"rows":[{"no":int,"kode":"product code exactly as printed",'
               '"qty":"carton / pcs exactly as printed","harga":"price exactly as printed",'
               '"jumlah":"row amount exactly as printed"}]}\n'
               'Use exact printed digits and Indonesian formats (1.234,56). Do NOT correct, '
               'infer, smooth or complete anything. Genuinely unclear digit = "?" inside the string. '
               'Output only the JSON.')
ITEM_PROMPT2 = ITEM_PROMPT.replace('Look ONLY', 'Re-read the table once more, then look ONLY')

def vision_ask(did, page, prompt, dpi=400, timeout=200):
    req = urllib.request.Request(f'{BASE}/documents/{did}/vision_ask',
        data=json.dumps({'page': page, 'prompt': prompt, 'dpi': dpi}).encode(),
        headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())['text']

def parse_rows(txt):
    txt = re.sub(r'^```(?:json)?|```$', '', txt.strip(), flags=re.M).strip()
    try:
        j = json.loads(txt)
    except Exception:
        m = re.search(r'\{.*\}', txt, re.S)
        if not m:
            return {}
        try:
            j = json.loads(re.sub(r',\s*([}\]])', r'\1', m.group(0)))
        except Exception:
            return {}
    out = {}
    for x in (j.get('rows') or []):
        k = str(x.get('kode', '')).strip()
        if k:
            out[k] = x
    return out

def doc_id_for(fn, corpus):
    for did, d in corpus.items():
        if (d.get('filename') or '').rsplit('.pdf', 1)[0].strip()[:28] == fn:
            return did
    return None

# ---------- P1/P2 per page ----------
def verify_page(fn, page, fp, corpus, ovr, pl, variant_sem):
    items_all = [r for r in fp if r[FP['src']] == f'p{page} {fn}' and r[FP['stat']] != 'SUMMARY']
    rev = [r for r in items_all if r[FP['stat']] in UNRES and rowkey(r) not in ovr]
    if not rev:
        return 0, 'done'
    foot = next((n2(r[12]) or n2(r[14]) for r in fp
                 if r[FP['src']] == f'p{page} {fn}' and r[FP['stat']] == 'SUMMARY'
                 and (n2(r[12]) or n2(r[14]))), None)
    did = doc_id_for(fn[:28] if len(fn) > 28 else fn, corpus) or doc_id_for(fn, corpus)
    if not did:
        return 0, 'no-docid'
    try:
        v1 = parse_rows(vision_ask(did, page, ITEM_PROMPT))
    except Exception as e:
        return 0, f'api:{e}'[:60]

    def subst_val(r, fld):
        rd = v1.get(str(r[0] or '').strip())
        if rd and '?' not in json.dumps(rd, ensure_ascii=False):
            v = rd.get(fld)
            if v not in (None, ''):
                return str(v)
        return r[FP[fld]] if fld in FP else r[{'qty': 4, 'harga': 5, 'jumlah': 11}[fld]]

    acc = 0
    # ---- page-proof: substitute ALL readings; Σjumlah == footer? ----
    if foot and all(v1.get(str(r[0] or '').strip()) or n2(r[FP['jumlah']]) is not None for r in items_all):
        try:
            s = sum(n2(subst_val(r, 'jumlah')) for r in items_all)
            ok = s is not None and abs(s - foot) <= max(5, foot * 0.002)
        except TypeError:
            ok = False
        if ok:
            changed = [r for r in rev if v1.get(str(r[0] or '').strip())]
            for r in items_all:
                k = rowkey(r)
                if r[FP['stat']] not in UNRES or k in ovr:
                    continue
                rd = v1.get(str(r[0] or '').strip()) or {}
                fields = {f: str(rd[f]) for f in ('qty', 'harga', 'jumlah') if rd.get(f) not in (None, '')}
                ovr[k] = dict(fields=fields, stage='P1-VISUAL',
                              evidence=f'vision re-read; page Σjumlah={s:,.0f} == printed DPP={foot:,.0f}',
                              at=time.strftime('%F %T'))
                acc += 1
            if acc:
                return acc, 'page-proof'
    # ---- per-row: promoted semantics, needs TWO agreeing reads ----
    semrows = [r for r in rev if variant_sem and r[FP['kemasan']] and KB.isi_per_carton(r[FP['kemasan']])]
    v2 = {}
    for r in semrows:
        kode = str(r[0] or '').strip()
        rd = v1.get(kode)
        if not rd or '?' in json.dumps(rd, ensure_ascii=False):
            continue
        isi = KB.isi_per_carton(r[FP['kemasan']])
        sem = variant_sem.get(f'{page}', None) if isinstance(variant_sem, dict) else variant_sem
        if row_fits(r, str(rd.get('qty', '')), str(rd.get('harga', '')), str(rd.get('jumlah', '')), sem, isi):
            if not v2:
                try:
                    v2 = parse_rows(vision_ask(did, page, ITEM_PROMPT2))
                except Exception:
                    v2 = {}
            r2 = v2.get(kode)
            if r2 and all(str(rd.get(f, '')) == str(r2.get(f, '')) for f in ('qty', 'harga', 'jumlah')):
                ovr[rowkey(r)] = dict(
                    fields={f: str(rd[f]) for f in ('qty', 'harga', 'jumlah') if rd.get(f) not in (None, '')},
                    stage='P2-AGREE', evidence=f'2 independent reads agree; fits {sem} (isi={isi})',
                    at=time.strftime('%F %T'))
                acc += 1
    return acc, 'ok'

# ---------- queue ----------
def build_queue(fp):
    ovr = load(OVR_P, {})
    todo, seen = [], set()
    for r in fp:
        src = r[FP['src']]
        if r[FP['stat']] in UNRES and src and rowkey(r) not in ovr:
            m = re.match(r'^p(\d+) (.*)$', src)
            if m and (m.group(2), int(m.group(1))) not in seen:
                seen.add((m.group(2), int(m.group(1))))
                todo.append((m.group(2), int(m.group(1))))
    return todo

def promoted_sem_per_doc(fp, kb):
    """sem per filename via its variant (best-effort global promoted)."""
    sems = {v.get('promoted_sem') for v in kb['variants'].values() if v.get('promoted_sem')}
    return next(iter(sems), None)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--p0', action='store_true')
    ap.add_argument('--once', type=int, default=0)
    ap.add_argument('--loop', action='store_true')
    ap.add_argument('--pages', type=int, default=25)
    ap.add_argument('--sleep', type=float, default=3.0)
    a = ap.parse_args()

    sheets = json.load(open(DUMP))['sheets']
    fp = sheets['Faktur Penjualan']
    corpus = json.load(open(CORPUS))
    pl = KB.build_page_lookup(corpus)
    kb = KB.load_kb()
    sem = promoted_sem_per_doc(fp, kb)

    if a.p0:
        print(f'P0 accepted {p0(fp)} rows (zero-API) | overrides {len(load(OVR_P, {}))}')
        return

    done_key = lambda fn, p: f'{fn}|{p}'
    def process(n):
        q = load(QUE_P, {'done_pages': []})
        done = set(q.get('done_pages', []))
        todo = [t for t in build_queue(fp) if done_key(*t) not in done]
        processed = 0
        for fn, p in todo[:n]:
            ovr = load(OVR_P, {})
            try:
                acc, why = verify_page(fn, p, fp, corpus, ovr, pl, sem)
            except Exception as e:
                acc, why = 0, f'err:{e}'[:60]
            save(OVR_P, ovr)
            done.add(done_key(fn, p))
            q = dict(done_pages=sorted(done), accepted_total=len(ovr), last=f'{fn} p{p}: +{acc} [{why}]')
            save(QUE_P, q)
            processed += 1
            print(f'  p{p} {fn[:26]}: +{acc} [{why}] | overrides {len(ovr)}', flush=True)
            time.sleep(a.sleep)
        return processed, q

    if a.once:
        process(a.once)
    elif a.loop:
        while True:
            processed, q = process(a.pages)
            remaining = len(build_queue(fp))
            print(f'ITER done | queue-remaining-pages={remaining}', flush=True)
            if remaining == 0 or processed == 0:
                # re-run P0 each round: new readings may complete more pages
                if p0(fp):
                    continue
                print('CONVERGED', flush=True)
                break
            time.sleep(a.sleep * 5)
    ovr = load(OVR_P, {})
    print(f"overrides total: {len(ovr)} | by stage: {dict(collections.Counter(v['stage'] for v in ovr.values()))}")

if __name__ == '__main__':
    main()
