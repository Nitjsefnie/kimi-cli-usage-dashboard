# Model attribution: wire-first, per-record

**Status:** approved (user gave standing authorization to proceed without per-section gates)
**Date:** 2026-07-17
**Supersedes:** the date-only cutoff scheme in `backend/parse.py` / `src/parser.js`

## Context

kimi-dash labels every billing record with a pricing model (`kimi-k2-6`,
`kimi-k2-7-code`, `kimi-k3`) and precomputes cost at parse time. Today that
label comes *only* from hardcoded date cutoffs applied to a session's **first
event**, and the wire's own model string is deliberately discarded.

That scheme is wrong in four independently-verified ways. This spec replaces it
with a per-record, wire-first assignment and keeps dates only where the wire
genuinely cannot answer.

## Evidence (all verified against the live corpus, not assumed)

| # | Finding | Evidence |
|---|---|---|
| 1 | The kimi-code wire **carries the model on the `usage.record` itself** — the exact record that becomes a billing row. | `L27 type=usage.record model='kimi-code/k3'` |
| 2 | **`K3_CUTOFF_EPOCH` is provably wrong.** Real k3 records predate it by 20m41s. | earliest k3 record `2026-07-16 14:45:55.471Z`; constant = `15:06:34Z` |
| 3 | **Model is not a property of a session.** Session `aed8326f` switches `kimi-for-coding` → `k3` mid-session, 24s apart. | L17 `kimi-for-coding` @14:45:34 → L27 `k3` @14:45:55 |
| 4 | Per-session labelling **leaks across the boundary**: DB has `kimi-k2-7-code` records at `2026-07-16 15:10:14`, after the cutoff, purely because their session started earlier. | `records` table |
| 5 | **Legacy-format transcripts carry no model string at all** — and are still being produced (`protocol_version 1.10`, dated `2026-07-16 18:25`). Dates are unavoidable for them. | R2 `sessions/2aa458d2…` |
| 6 | `kimi-code/kimi-for-coding` is **ambiguous**: it covers both k2.6 and k2.7-code (branding didn't change at that transition). | only two distinct strings exist corpus-wide |

Only two model strings exist across all 67 local sessions:
`kimi-code/kimi-for-coding` and `kimi-code/k3`.

## Decision

**The wire string decides when it is present and unambiguous; dates decide only
what the wire cannot express; both are evaluated per-record.**

Resolution ladder, applied to **each** billing record:

1. Wire model string says `k3` → `kimi-k3`. **Regardless of date.**
2. Wire model string says `kimi-for-coding` → ambiguous era → the **record's own
   timestamp** vs `MODEL_CUTOFF_EPOCH` picks `kimi-k2-6` (before) or
   `kimi-k2-7-code` (at/after). `K3_CUTOFF_EPOCH` is **not** consulted — a wire
   that says `kimi-for-coding` is not k3, whatever the date.
3. No/unrecognized model string (legacy format) → full date ladder on the
   record's own timestamp, exactly as today (both cutoffs).
4. No timestamp either → `kimi-k2-7-code` (unchanged fallback).

This fixes the user's reported bug **by construction**: k2.7-code used after the
K3 cutoff hits rule 2 and stays `kimi-k2-7-code`. And k3 used *before* the
cutoff hits rule 1 and is correctly `kimi-k3`.

### Why not "delete the date cutoffs entirely"

Rejected: finding 5. The legacy CLI is still live and emits no model string; k2.6
history is entirely legacy. Dates are the only available signal there.

### Why not "keep dates, just fix the constant"

Rejected: it cannot express finding 3 (mid-session switch) and cannot ever
distinguish post-cutoff k2.7 from k3 (the reported bug). A date is a proxy for a
fact the wire states outright.

## Changes

### `backend/parse.py`

- Replace `_model_for(first_event_ts)` with
  `_model_for(wire_model: str | None, ts: datetime | None)` implementing the
  4-rule ladder above.
- Add a normalization step mapping raw provider ids → canonical pricing labels.
  **Must** run before pricing (see the `rate_for` trap below).
- Correct `K3_CUTOFF_EPOCH`: `1784214394` → `1784213155`
  (`2026-07-16 14:45:55 UTC`, the earliest observed k3 record). Now only
  affects model-less legacy records.
- Legacy path (~L198): call with `wire_model=None`, `ts=ts_dt` (the record's
  ts), not `first_event_ts`.
- kimi-code path (~L476): call with `wire_model=obj.get("model")`, `ts=ts_dt`.
- Retain `first_event_ts` only as the per-record fallback when a record has no
  timestamp of its own.
- Rewrite the `_model_for` docstring: the "wire strings are deliberately
  ignored" rationale is now false and must not survive.

### `backend/pricing.py`

No rate changes. **Constraint to respect:** `rate_for` matches by substring
against canonical keys, so a raw `"kimi-code/k3"` passed in would match *no*
key and silently fall back to `DEFAULT_RATES` (k2-6 — a ~3x undercount). Only
canonical labels may reach `compute_cost`.

### `src/parser.js`

Mirror exactly: same ladder, same corrected constant, same normalization,
per-record. The two parsers must not drift.

### Version bump

- `backend/.env.example`: `PARSER_VERSION=5` → `6`
- `src/parser.js`: `window.PARSER_VERSION = "5"` → `"6"`
- `.env` (live, untracked): bump to `6` so ingest reparses.

This is a parse-algorithm change, so every `files` row must be invalidated.

### `tests/test_parse.py`

Two existing tests **encode the bug** and must be inverted:

- `test_kimi_code_post_k3_cutoff_is_coerced_to_k3` — asserts a wire record
  saying `kimi-for-coding` after the cutoff bills as k3. This *is* the reported
  bug. Invert: it must stay `kimi-k2-7-code`.
- `test_kimi_code_raw_provider_model_is_coerced_by_date` — asserts the wire
  string is ignored. Invert: the wire string is authoritative.

New tests required:

1. `kimi-code/k3` **before** `K3_CUTOFF` → `kimi-k3` (finding 2).
2. `kimi-for-coding` **after** `K3_CUTOFF` → `kimi-k2-7-code` (the reported bug).
3. Mid-session switch: one file, `kimi-for-coding` then `k3` → two records with
   **different** labels (finding 3).
4. Legacy (no model string) → date ladder unchanged; pre/post `MODEL_CUTOFF`.
5. Legacy after `K3_CUTOFF` → `kimi-k3` (no regression on the 16 real files).
6. No timestamp → `kimi-k2-7-code`.
7. Cost for a `kimi-code/k3` record uses k3 rates, not `DEFAULT_RATES` — guards
   the `rate_for` substring trap directly.

## Verification

- `pytest tests/` green.
- Reparse and confirm the DB no longer shows `kimi-k2-7-code` records after
  `2026-07-16 15:06:34`, and that k3 records now begin at `14:45:55` rather
  than `15:14:56`.
- Confirm session `aed8326f` yields both labels.
- Confirm the 16 legacy files at `18:16–18:25` still label `kimi-k3`.

## Risks

| Risk | Sev | Mitigation |
|---|---|---|
| Raw provider id reaches `compute_cost` → silent 3x undercount via `DEFAULT_RATES`. | HIGH | Normalize before pricing; test 7 asserts it. |
| `parse.py` and `parser.js` drift. | MED | Ship together; mirror tests. |
| Legacy records remain model-ambiguous — a legacy session on k2.7 after the K3 date still mislabels as k3. | MED | Accepted, documented. Unfixable without a model string; unchanged from today (no regression). |
| Corrected `K3_CUTOFF` is "earliest observed", not a vendor-published launch instant. | LOW | Strictly better than a provably-wrong value; documented as empirical. |

## Out of scope

- **Backfilling a model string into legacy transcripts.** Not possible.
- **Re-deriving `MODEL_CUTOFF_EPOCH`.** Its to-the-second precision is fiction —
  k2.6 records stop `2026-06-10 22:42`, k2.7 starts `2026-06-12 15:23`, so the
  constant sits in a 41-hour data gap. Any value in that window is equally
  defensible; leave it.
- **Rate-table changes.** Cost math is unchanged; only labels move.
