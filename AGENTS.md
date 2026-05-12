# kimi-dash

@README.md

## Repo orientation

- `backend/` — FastAPI app.
  - `app.py` — startup/shutdown, route mounting, `/` static, asset cache-bust.
  - `api.py` — REST endpoints (`/api/me`, `/api/projects`, `/api/dashboard`,
    `/api/cache`, `/api/context-growth/{agg,session}`, `/api/sessions*`,
    `/api/events` SSE).
  - `parse.py` — wire.jsonl → records + ctx_turns. Mirrors
    the canonical `~/.kimi/scripts/parse_wire.py` for turn-based
    StatusUpdate extraction.
  - `pricing.py` — single source of truth for Kimi K2.6 rates. Bump
    `PARSER_VERSION` in `.env` whenever this changes.
  - `ingest.py` — R2 walk, etag/parser-version reparse decision, persistence
    in two-phase transactions, broadcasts `ingest_done` SSE on success.
  - `r2.py` — S3 client with `file://` filesystem-mirror fallback for dev.
  - `auth.py`, `login.py`, `session.py` — PBKDF2 verification against the
    external auth DB's `users.config`, HMAC-signed session cookies, plus
    a guest-mode sentinel (`user_id=0`, per-process secret).
  - `events.py` — thread-safe SSE broadcaster.
  - `db.py` — two psycopg pools: `viz_pool` (kimi_viz) and `auth_pool`
    (read-only auth DB). Pools never join across DBs.
  - `cache.py` — in-memory LRU for raw transcript bytes.
  - `schema.sql` — idempotent `CREATE TABLE IF NOT EXISTS` + safe
    `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` migrations.

- `public/` — `index.html`, `app.css`. Served at `/`. Backend rewrites
  `index.html` on each request to inject `window.BACKEND_URL`,
  `window.IS_GUEST`, and mtime-based `?v=` query strings on every static
  asset reference.

- `src/` — React JSX modules served at `/src/*` (in-browser Babel, no
  build step).
  - `app.jsx` — top-level shell, routing, dashboard fetcher, SSE listener.
  - `parser.js` — in-browser wire.jsonl parser used by the Inspector.
    Pricing table here MUST match `backend/pricing.py`.
  - `dashboard-charts.jsx`, `dashboard-charts-extra.jsx` — SVG panels.
  - `views/` — `cache-view.jsx`, `context-growth-view-v2.jsx`.

- `scripts/` — symlinks to canonical `~/.kimi/scripts/*.py`. **Read-only**;
  the web app does NOT invoke them at runtime.

- `tests/` — pytest suite.

- `fixtures/` — small JSONL + zip samples for parser and API tests.

## Conventions

- **Cost uses Kimi K2.6 rates**. `cache_creation` is billed at a flat rate
  (no TTL split in Kimi wire format).
- **Cross-file uuid dedup happens at READ time** via `DISTINCT ON (uuid)`
  in `/api/dashboard`, `/api/cache`, etc. There is no persisted Phase 2
  rollup table.
- **Don't invoke `~/.kimi/scripts/parse_wire.py`** at runtime, and
  don't edit it from this repo. If the canonical Python and our port
  drift, fix it here, not there.
- **Tests use fixtures, not real R2.** The R2 client supports
  `R2_ENDPOINT=file:///path/to/mirror/` for offline dev.
- **Parser version invalidation:** Bump `PARSER_VERSION` in `.env` whenever
  parser semantics or `pricing.py` rates change — every file reparses on
  next ingest.
- **In-browser fallback retained:** The drag-drop FileReader path in
  `src/app.jsx` stays as an offline fallback. No upload endpoint exists.

## Operations

- Manual ingest: `POST /admin/ingest` with `X-Admin-Token: $ADMIN_TOKEN`.
- Bump `PARSER_VERSION` in `.env` whenever parser semantics or
  `pricing.py` rates change — every file reparses on next ingest.
