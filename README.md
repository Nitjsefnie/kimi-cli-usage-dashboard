# kimimeter

A self-hosted web application that visualises Kimi Code session JSONL transcripts.

## Overview

**kimimeter** ingests transcripts from Cloudflare R2 (or a local `file://` mirror), parses them into Postgres, and serves dashboards and raw transcripts to a React frontend rendered via in-browser Babel (no npm/build step).

The dashboard panels include: Session Burn Rate, Cost by Model, Token Breakdown, Prompt-Cache Split, Per-Session Context Growth, Response Sizes, Tool Usage Ratio, Reply Latency, and Tool Error Rate.

## Technology stack

- **Backend**: Python 3.13+, FastAPI, Uvicorn, psycopg3 (with connection pooling)
- **Frontend**: React 18 (loaded from CDN), in-browser Babel transpilation, vanilla JS/JSX — no webpack, vite, or npm install
- **Database**: PostgreSQL (two separate DBs: `kimi_viz` for app data, external auth DB for user credentials)
- **Object storage**: Cloudflare R2 via S3-compatible API, or local filesystem mirror (`file://`)
- **Scheduling**: APScheduler (BackgroundScheduler) for hourly ingest
- **Serialization**: orjson for fast JSON parsing
- **Testing**: pytest, pytest-asyncio, httpx (for TestClient)
- **Deployment**: systemd service

## Project structure

```
backend/          — FastAPI application
  app.py          — Startup/shutdown, route mounting, static asset serving
  api.py          — REST endpoints
  parse.py        — wire.jsonl → records + ctx_turns
  pricing.py      — Kimi K2.6 / K2.7 Code rates
  ingest.py       — R2 walk, etag/parser-version reparse decision
  r2.py           — S3 client with file:// fallback for dev
  auth.py         — PBKDF2-SHA256 helpers
  login.py        — /login GET/POST, /logout, /login/guest
  session.py      — HMAC-signed session cookies
  events.py       — Thread-safe SSE broadcaster
  db.py           — Two psycopg pools
  cache.py        — In-process LRU for raw transcript bytes
  schema.sql      — Idempotent CREATE TABLE + migrations

public/           — Static assets served at /
  index.html      — Bootstraps React, Babel, JSZip from CDN
  app.css         — Dark-theme dashboard styles

src/              — React JSX modules served at /src/*
  app.jsx         — Top-level shell, routing, dashboard fetcher
  parser.js       — In-browser wire.jsonl parser
  dashboard-charts.jsx      — Core SVG panels
  dashboard-charts-extra.jsx — Additional panels
  context-growth-view.jsx    — Context growth visualisation
  detail-pane.jsx            — Session detail / inspector panes
  event-helpers.jsx          — Shared event formatting helpers
  synthetic-data.js          — Synthetic dashboard data generator
  views/
    cache-view.jsx
    context-growth-view-v2.jsx

scripts/          — Symlinks to canonical ~/.kimi/scripts/*.py

tests/            — pytest suite
fixtures/         — Small JSONL + zip samples
examples/         — Sample systemd service file
```

## Setup

```bash
# 1. Create the app database and apply schema
createdb kimi_viz
psql kimi_viz -f backend/schema.sql

# 2. Configure environment
cp backend/.env.example .env
# Edit .env to set real DATABASE_URL_VIZ, DATABASE_URL_AUTH, R2_*, ADMIN_TOKEN

# 3. Create virtualenv and install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
```

## Run the server

```bash
python3 -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

## Run tests

```bash
python3 -m pytest tests/ -q
```

## Manual operations

```bash
# Force an out-of-band ingest run
curl -X POST http://127.0.0.1:8000/admin/ingest \
  -H "X-Admin-Token: $ADMIN_TOKEN"
```
