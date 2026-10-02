# Design Doc — OCR Settings UI (provider-agnostic, hot-reload)

Status: **APPROVED 2026-09-20** (user: "add documentation in github then build and test"). Decisions: D1 hot-reload config file (no docker restart), D2 optional PIN gate, D3 model free-text + Fetch-models helper, D4 test must pass before Save (with explicit force override).

## Problem

OCR model/endpoint/key were env-only (`QWEN_MODEL` etc.) → changing them required shell + `docker compose up -d extractor`. Goal: a small admin page to change **API key, model, endpoint URL** (not Qwen-locked) without any restart.

## Architecture decision: config file + mtime hot-reload, NOT container recreation

- Auto-recreating the container from inside itself needs the docker socket mounted in the app container (security hole) and `docker restart` would not even re-read env (baked at create time; only `up -d` recreates). Fragile.
- Instead: runtime OCR settings live in `~/doc-pipeline/data/ocr_config.json` (mounted volume, mode 600). The extractor checks file mtime per request → change is effective **immediately, zero restarts**.
- Precedence: `ocr_config.json` (UI-managed) > env (`QWEN_API_KEY`, `QWEN_MODEL`, `QWEN_URL`, `OCR_ENGINE`) > code defaults. On first boot with no file, env seeds the page; saving from the UI takes ownership.
- Env vars remain the disaster-recovery path (delete file → env works again).

## API surface (extractor — the single service, all triggers go through it)

| Route | Purpose |
|---|---|
| `GET /view/settings` | HTML form page |
| `GET /settings/ocr` | current config, key **masked** (prefix + length + sha8; full value never leaves the server) |
| `POST /settings/ocr` | validate + test (unless `force`) + persist; returns masked summary |
| `POST /settings/ocr/test` | try a *draft* config (saved key used if key field blank) — no persist |
| `POST /settings/ocr/models` | proxy `GET {endpoint}/models` → model list for the picker |

Endpoint normalization: user may paste base (`…/v1`, `…/compatible-mode/v1`) or full chat-completions URL — the app appends `/chat/completions` when missing.

## "Test connection" semantics (why it matters)

Saves the real failure classes we hit during bring-up, in words:
- `401/403` → key rejected by provider
- `404` → model not available in this plan (this is exactly how we discovered `qwen-vl-ocr` isn't in Token Plan)
- connect/timeout → wrong endpoint URL or network
Test sends a tiny solid-black PNG asking the model to reply `ok` — exercises vision auth + model availability without meaningful token cost.

## Security posture

1. Config file 600, under `data/` (gitignored); API key never appears in responses, logs, or HTML (masked).
2. `SETTINGS_PIN` env (optional): when set, POST /settings/ocr requires matching pin. When unset the page relies on network binding — deploy on localhost/Tailscale only. Never expose :5000 to the public internet with this page enabled (whoever can save can point the server at any URL + own key; on a trusted single-admin network this is acceptable).
3. No SSRF-free pass: endpoint field is intentionally admin-controlled; document, don't whitelist.

## Files changed at build

| File | Change |
|---|---|
| `extractor/app.py` | `load_ocr_config()` (mtime cache), settings HTML + 3 API routes, `api_vision_ocr()` reads config (rename from `qwen_vision_ocr`), engine default from config |
| `docker-compose.yml` | mount `./data:/data` (config lands at `/data/ocr_config.json`); optional `SETTINGS_PIN` passthrough |
| `README.md` §Config/Runbook | settings page replaces env-edit as primary path |
| `USAGE.md` | new admin section |
| `TESTING.md` U13 / playbook T9 | live test cases incl. 401/404 surfacing |
| `.gitignore` | ensure `data/` (incl. `data/ocr_config.json`) excluded |

## Compatibility

`engine:"qwen"` payload value stays valid (means "API vision engine", provider-agnostic). Delivered JSON keeps `ocr_engine:"qwen"` + adds `ocr_model:<active>` so audits show which model produced each document.
