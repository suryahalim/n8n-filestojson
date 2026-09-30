# Deep-Verify Design — loop until converged, proof before value

Decided 2026-09-30 (user directive): *"i don't want to limit in one single api call
of LLM/agents to finish the task. i don't mind waiting and you call loops for a long
time to finish the job as long as it is accurate."*

## Principle
A single model call is an observation, not a truth. Accuracy comes from **escalating
evidence stages** and a **deterministic gate**: a number only enters the sheet when
arithmetic PROVES it. Everything else stays REVIEW with all readings attached.
Runtime and token cost are explicitly NOT constraints; wall-clock loops are fine.

## Stages (cheapest first, each stage only upgrades what its gate proves)
| Stage | API calls | Gate | Label |
|---|---|---|---|
| P0 page corroboration | 0 | Σitem(jumlah) == printed [PAGE TOTAL]/DPP footer of the same page | PASS-DEEP (P0-SUM) |
| P1 targeted vision re-read | 1/page | whole-page re-read substitutes into the SAME footer-sum proof, or promoted KB semantics fits exactly | PASS-DEEP (P1-VISUAL) |
| P2 second independent read | +1/page | two reads agree on qty+harga+jumlah AND arithmetic gate passes | PASS-DEEP (P2-AGREE) |
| P3 cross-document (planned) | 0 | booklet footer DPP/PPN == Faktur Pajak tab row for the same SOR | PASS-DEEP (P3-CROSSDOC) |
| KB self-learning | 0 | accepted fixes feed `knowledge/variants.json` conf rules & semantics | permanent |
Unprovable rows end as REVIEW with the raw vision readings stored in
`knowledge/overrides.json` history — a human decides with full context, never a guess.

## Components
- `extractor/app.py` — `POST /documents/{id}/vision_ask {page,prompt,dpi}`: renders the
  STORED original page (pdftoppm, default 400 dpi — sharper than the 200 dpi OCR pass)
  and sends ONE OpenAI-compatible vision call with an arbitrary prompt. Evidence tool
  only; never mutates documents. Reuses `/view/settings` OCR config (hot-reload).
- `scripts/deep_verify.py` — the loop engine. `--p0` (free stage), `--once N`,
  `--loop` (daemon until CONVERGED). Strict-JSON contract, `?` = admit unclear
  (a reading containing `?` is never trusted). Resumable checkpoint:
  `knowledge/dv_queue.json` (done pages, counters); results:
  `knowledge/overrides.json` keyed by `FP|<src>|<kode>|<desc24>` →
  `{fields:{qty,harga,jumlah}, stage, evidence, at}`.
- `scripts/batch_map.py` — applies overrides before writing: row fields replaced
  ONLY for REVIEW rows, status → `PASS-DEEP`, confidence column carries the stage +
  one-line evidence (auditable inside the sheet itself).

## Ordering pipeline (final state of a row)
raw OCR → arithmetic gate → KB promoted semantics (PASS-LEARNED) → digit-conf rules
(PASS-LEARNED-CORR) → deep-verify (PASS-DEEP) → human (REVIEW). Every upgrade keeps
`Source Page` provenance and writes WHY into Confidence — the sheet can always be
re-derived from evidence, and evidence is durable in git.

## Cost / pace (measured)
~25–45 s/page (render 400dpi + 1–2 vision calls). Queue after P0 = 192 pages /
1.237 rows → full convergence ≈ 2–3 h wall-clock, sequential, CPU-light — matches
the one-at-a-time host constraint. Cron-able; idempotent (overrides keyed, pages
checkpointed); re-running after new uploads only processes the delta.

## Current run (2026-09-30)
P0 converted 1.798 of 3.035 unresolved rows for FREE (59%) — booklet pages whose
item rows sum exactly to the printed DPP. Loop daemon processing the remaining
192 pages; sheet updated after convergence with one batch_map write + read-back
verify. Status column distribution will be reported then — expected honest tail:
handwritten/garbled digits that refuse both reads, kept in REVIEW by design.
