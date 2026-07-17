# Model Attribution (wire-first, per-record) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Label each billing record from the wire's own model string when it says so, falling back to date cutoffs only for transcripts that carry no model string.

**Architecture:** A single resolution ladder, `_model_for(wire_model, ts)`, replaces the per-session, date-only `_model_for(first_event_ts)`. It normalizes a raw provider id (`kimi-code/k3`) to a canonical pricing label (`kimi-k3`) before pricing ever sees it. Both parse paths (legacy and kimi-code) call it per-record with that record's own timestamp. `src/parser.js` mirrors it exactly.

**Tech Stack:** Python 3 (orjson, pytest), vanilla JS frontend, PostgreSQL, R2.

**Spec:** `docs/superpowers/specs/2026-07-17-model-attribution-design.md`

## Global Constraints

- Canonical pricing labels are exactly `kimi-k3`, `kimi-k2-7-code`, `kimi-k2-6`. Only these may reach `pricing.compute_cost`.
- `pricing.rate_for` matches by **substring** against those keys. A raw id like `kimi-code/k3` matches **no** key and silently returns `DEFAULT_RATES` (k2-6, a ~3x undercount). Normalize before pricing, always.
- `backend/parse.py` and `src/parser.js` are mirrors. Any change to one lands in the other in the same commit.
- Boundary rule is unchanged: strictly-before / inclusive-at. The cutoff instant belongs to the NEWER model.
- `MODEL_CUTOFF_EPOCH = 1781217035` is **not** changed by this plan.
- `K3_CUTOFF_EPOCH` becomes `1784213155` (`2026-07-16 14:45:55 UTC`).
- No rate-table values change.
- Every commit ends with: `Co-Authored-By: Kimi K2.6 <noreply@kimi.com>` (the implementing subagent's own trailer — one primary-author trailer per commit).

---

### Task 1: The resolution ladder

**Files:**
- Modify: `backend/parse.py:21-53` (constants + `_model_for`)
- Test: `tests/test_parse.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `parse._model_for(wire_model: str | None, ts: datetime | None) -> str` and `parse._canonical_model(wire_model: str | None) -> str | None`. Tasks 2 and 3 call `_model_for` with these exact parameter names and order. `parse.K3_CUTOFF_EPOCH`, `parse.K3_CUTOFF_DT`, `parse.MODEL_CUTOFF_EPOCH`, `parse.MODEL_CUTOFF_DT` keep their existing names.

`_canonical_model` returns `"kimi-k3"` for a raw id whose last `/`-segment is `k3`, `None` for `kimi-for-coding` (meaning "ambiguous — ask the date"), and `None` for anything unrecognized or falsy. Because both the ambiguous and unrecognized cases return `None`, `_model_for` distinguishes them by re-inspecting the string: this is why `_model_for` takes the raw `wire_model`, not the normalized value.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_parse.py`, after `test_post_k3_cutoff_cost_uses_k3_rates`:

```python
def test_canonical_model_maps_k3_provider_id():
    assert parse._canonical_model("kimi-code/k3") == "kimi-k3"


def test_canonical_model_returns_none_for_ambiguous_and_unknown():
    # kimi-for-coding spans both k2.6 and k2.7-code: the wire cannot resolve it.
    assert parse._canonical_model("kimi-code/kimi-for-coding") is None
    assert parse._canonical_model("some/unknown-model") is None
    assert parse._canonical_model(None) is None
    assert parse._canonical_model("") is None


def test_model_for_k3_wire_string_beats_an_earlier_date():
    """A wire that says k3 is k3, even before K3_CUTOFF_EPOCH. Real k3 records
    predate the constant by ~20 minutes.
    """
    ts = datetime.fromtimestamp(parse.K3_CUTOFF_EPOCH - 3600, tz=timezone.utc)
    assert parse._model_for("kimi-code/k3", ts) == "kimi-k3"


def test_model_for_kimi_for_coding_never_becomes_k3():
    """The reported bug: k2.7-code is still selectable after the K3 cutoff.
    A wire that says kimi-for-coding is not k3, whatever the date.
    """
    ts = datetime.fromtimestamp(parse.K3_CUTOFF_EPOCH + 86400, tz=timezone.utc)
    assert parse._model_for("kimi-code/kimi-for-coding", ts) == "kimi-k2-7-code"


def test_model_for_kimi_for_coding_uses_model_cutoff_for_the_k2_era():
    before = datetime.fromtimestamp(parse.MODEL_CUTOFF_EPOCH - 1, tz=timezone.utc)
    at = datetime.fromtimestamp(parse.MODEL_CUTOFF_EPOCH, tz=timezone.utc)
    assert parse._model_for("kimi-code/kimi-for-coding", before) == "kimi-k2-6"
    assert parse._model_for("kimi-code/kimi-for-coding", at) == "kimi-k2-7-code"


def test_model_for_without_wire_string_uses_the_full_date_ladder():
    """Legacy transcripts carry no model string; dates are all we have."""
    k26 = datetime.fromtimestamp(parse.MODEL_CUTOFF_EPOCH - 1, tz=timezone.utc)
    k27 = datetime.fromtimestamp(parse.K3_CUTOFF_EPOCH - 1, tz=timezone.utc)
    k3 = datetime.fromtimestamp(parse.K3_CUTOFF_EPOCH, tz=timezone.utc)
    assert parse._model_for(None, k26) == "kimi-k2-6"
    assert parse._model_for(None, k27) == "kimi-k2-7-code"
    assert parse._model_for(None, k3) == "kimi-k3"


def test_model_for_without_timestamp_falls_back_to_k2_7_code():
    assert parse._model_for(None, None) == "kimi-k2-7-code"
    assert parse._model_for("kimi-code/kimi-for-coding", None) == "kimi-k2-7-code"


def test_k3_cutoff_matches_earliest_observed_k3_record():
    """1784213155 == 2026-07-16 14:45:55 UTC, the earliest k3 usage.record in
    the corpus. The prior value (1784214394) postdated real k3 usage.
    """
    assert parse.K3_CUTOFF_EPOCH == 1784213155
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /root/kimi-dash && python3 -m pytest tests/test_parse.py -k "canonical_model or model_for or k3_cutoff_matches" -v`
Expected: FAIL — `AttributeError: module 'backend.parse' has no attribute '_canonical_model'`, and `_model_for()` taking 1 positional argument.

- [ ] **Step 3: Implement the ladder**

In `backend/parse.py`, replace the comment block + constants at lines 21-34 with:

```python
# Model attribution, oldest first. Each constant is a frozen UTC epoch, NOT a
# live expression.
#
# The wire is the primary source: a kimi-code usage.record carries the provider
# id (e.g. "kimi-code/k3") on the record that becomes a billing row, so it is
# authoritative when it names a model unambiguously.
#
# Dates are the fallback, needed for two real cases the wire cannot express:
#   - Legacy-format transcripts carry NO model string at all (and are still
#     being produced).
#   - "kimi-for-coding" spans BOTH k2.6 and k2.7-code — the branding did not
#     change at that transition — so only a date can separate those two.
#
# Boundaries are strictly-before / inclusive-at, so each cutoff instant
# belongs to the NEWER model.
MODEL_CUTOFF_EPOCH = 1781217035   # 2026-06-11 22:30:35 UTC  k2-6 -> k2-7-code
K3_CUTOFF_EPOCH = 1784213155      # 2026-07-16 14:45:55 UTC  earliest observed
                                  # k3 usage.record; only applies to records
                                  # with no wire model string.
MODEL_CUTOFF_DT = datetime.fromtimestamp(MODEL_CUTOFF_EPOCH, tz=timezone.utc)
K3_CUTOFF_DT = datetime.fromtimestamp(K3_CUTOFF_EPOCH, tz=timezone.utc)

# Raw provider id -> canonical pricing label. Only ids that identify a pricing
# model on their own belong here; "kimi-for-coding" deliberately does not.
_WIRE_MODEL_MAP = {"k3": "kimi-k3"}

# Raw provider ids that name a real model but cannot pin a pricing label alone.
_AMBIGUOUS_WIRE_MODELS = frozenset({"kimi-for-coding"})
```

Then replace `_model_for` (lines 37-53) with:

```python
def _canonical_model(wire_model: str | None) -> str | None:
    """Raw provider id -> canonical pricing label, or None if the wire cannot
    settle it alone (ambiguous id, unrecognized id, or no id at all).

    Only canonical labels may reach pricing.compute_cost: pricing.rate_for
    matches by substring, so a raw "kimi-code/k3" would match no key and
    silently bill at DEFAULT_RATES (k2-6) — a ~3x undercount.
    """
    if not wire_model:
        return None
    return _WIRE_MODEL_MAP.get(wire_model.rsplit("/", 1)[-1])


def _model_for(wire_model: str | None, ts: datetime | None) -> str:
    """Pricing model for ONE billing record.

    The wire wins when it names a model unambiguously; dates decide only what
    the wire cannot express. Resolved per-record, not per-session: a session
    can switch model mid-flight (observed: kimi-for-coding -> k3, 24s apart).

    A record with no usable timestamp falls back to kimi-k2-7-code, NOT the
    newest label: an unstamped record is already-ingested history, so it cannot
    postdate the K3 cutoff, and K3's rates are ~3x higher.
    """
    canonical = _canonical_model(wire_model)
    if canonical is not None:
        return canonical

    if ts is None:
        return "kimi-k2-7-code"

    if ts < MODEL_CUTOFF_DT:
        return "kimi-k2-6"

    # An ambiguous id names a real, non-k3 model: the date only separates the
    # k2 generations, and must never promote it to k3.
    if wire_model and wire_model.rsplit("/", 1)[-1] in _AMBIGUOUS_WIRE_MODELS:
        return "kimi-k2-7-code"

    if ts < K3_CUTOFF_DT:
        return "kimi-k2-7-code"
    return "kimi-k3"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /root/kimi-dash && python3 -m pytest tests/test_parse.py -k "canonical_model or model_for or k3_cutoff_matches" -v`
Expected: PASS (9 tests). Other tests in the file will still fail — Tasks 2 and 3 fix the call sites.

- [ ] **Step 5: Commit**

```bash
cd /root/kimi-dash
git add backend/parse.py tests/test_parse.py
git commit -m "feat(parse): wire-first per-record model resolution ladder

The wire carries the provider id on the usage.record that becomes a
billing row, so it is authoritative when unambiguous. Dates now only
cover what the wire cannot express: legacy transcripts with no model
string, and kimi-for-coding spanning both k2 generations.

Also corrects K3_CUTOFF_EPOCH: real k3 records predate the old value
(15:06:34) by 20m41s. Earliest observed is 14:45:55.

Co-Authored-By: Kimi K2.6 <noreply@kimi.com>"
```

---

### Task 2: Wire the kimi-code path

**Files:**
- Modify: `backend/parse.py` (the `usage.record` branch in `_parse_kimi_code`, around line 476)
- Test: `tests/test_parse.py`

**Interfaces:**
- Consumes: `parse._model_for(wire_model, ts)` from Task 1.
- Produces: nothing new. Records emitted from `_parse_kimi_code` carry a canonical `model`.

The `usage.record` object holds the id at top level: `{"type":"usage.record","time":…,"model":"kimi-code/k3","usage":{…}}`. `ts_dt` is already computed in scope.

- [ ] **Step 1: Write the failing tests**

`test_kimi_code_post_k3_cutoff_is_coerced_to_k3` currently asserts the bug. **Replace that whole test** with:

```python
def test_kimi_code_post_k3_cutoff_keeps_kimi_for_coding_as_k2_7_code():
    """k2.7-code is still selectable after the K3 cutoff. A wire that says
    kimi-for-coding must NOT be repriced at K3's ~3x rates.
    """
    ms = int((parse.K3_CUTOFF_EPOCH + 10) * 1000)
    blob = (
        b'{"type":"metadata","protocol_version":"1.4","created_at":%d}\n'
        b'{"type":"turn.prompt","time":%d,"input":[{"type":"text","text":"Hi"}],'
        b'"origin":{"kind":"user"}}\n'
        b'{"type":"usage.record","time":%d,"model":"kimi-code/kimi-for-coding",'
        b'"usage":{"inputOther":1000,"output":200,"inputCacheRead":100,'
        b'"inputCacheCreation":50}}\n'
    ) % (ms, ms + 1000, ms + 2000)
    out = parse.parse_file("sessions/projKC/sess-k3/wire.jsonl", blob)
    r = out["records"][0]
    assert r["model"] == "kimi-k2-7-code"
    expected_cost = pricing.compute_cost(
        "kimi-k2-7-code",
        fresh=1000, create=50, read=100, output=200,
    )
    assert r["cost_usd"] == pytest.approx(expected_cost, rel=1e-9)
```

Then append these:

```python
def test_kimi_code_k3_wire_string_before_cutoff_is_k3_at_k3_rates():
    """Guards the pricing.rate_for substring trap: a raw "kimi-code/k3" must
    be normalized before pricing, or it silently bills at DEFAULT_RATES (k2-6).
    """
    ms = int((parse.K3_CUTOFF_EPOCH - 3600) * 1000)
    blob = (
        b'{"type":"metadata","protocol_version":"1.4","created_at":%d}\n'
        b'{"type":"turn.prompt","time":%d,"input":[{"type":"text","text":"Hi"}],'
        b'"origin":{"kind":"user"}}\n'
        b'{"type":"usage.record","time":%d,"model":"kimi-code/k3",'
        b'"usage":{"inputOther":1000,"output":200,"inputCacheRead":100,'
        b'"inputCacheCreation":50}}\n'
    ) % (ms, ms + 1000, ms + 2000)
    out = parse.parse_file("sessions/projKC/sess-k3early/wire.jsonl", blob)
    r = out["records"][0]
    assert r["model"] == "kimi-k3"
    expected_cost = pricing.compute_cost(
        "kimi-k3",
        fresh=1000, create=50, read=100, output=200,
    )
    assert r["cost_usd"] == pytest.approx(expected_cost, rel=1e-9)
    assert r["cost_usd"] != pytest.approx(
        pricing.compute_cost(
            "kimi-k2-6", fresh=1000, create=50, read=100, output=200
        ),
        rel=1e-9,
    )


def test_kimi_code_mid_session_model_switch_labels_records_independently():
    """Observed in session aed8326f: kimi-for-coding -> k3, 24s apart. A
    per-session label cannot represent this.
    """
    ms = int((parse.K3_CUTOFF_EPOCH - 3600) * 1000)
    blob = (
        b'{"type":"metadata","protocol_version":"1.4","created_at":%d}\n'
        b'{"type":"turn.prompt","time":%d,"input":[{"type":"text","text":"Hi"}],'
        b'"origin":{"kind":"user"}}\n'
        b'{"type":"usage.record","time":%d,"model":"kimi-code/kimi-for-coding",'
        b'"usage":{"inputOther":1000,"output":200,"inputCacheRead":100,'
        b'"inputCacheCreation":50}}\n'
        b'{"type":"usage.record","time":%d,"model":"kimi-code/k3",'
        b'"usage":{"inputOther":1000,"output":200,"inputCacheRead":100,'
        b'"inputCacheCreation":50}}\n'
    ) % (ms, ms + 1000, ms + 2000, ms + 26000)
    out = parse.parse_file("sessions/projKC/sess-switch/wire.jsonl", blob)
    assert len(out["records"]) == 2
    assert [r["model"] for r in out["records"]] == ["kimi-k2-7-code", "kimi-k3"]
```

Also fix the two now-false rationales. Rename `test_kimi_code_raw_provider_model_is_coerced_by_date` to `test_kimi_code_raw_provider_model_is_honored` and replace its docstring with:

```python
    """kimi-code usage.record embeds the raw provider id. "kimi-for-coding" is
    ambiguous across the k2 generations, so the record's date picks between
    them — this fixture is stamped 2026-07-02, i.e. the k2.7-code era.
    """
```

Rename `test_kimi_code_pre_cutoff_raw_provider_model_is_coerced_to_k2_6` to `test_kimi_code_pre_cutoff_ambiguous_model_resolves_to_k2_6` and replace its docstring with:

```python
    """"kimi-for-coding" cannot distinguish k2.6 from k2.7-code on its own, so
    a record before MODEL_CUTOFF_EPOCH resolves to kimi-k2-6.
    """
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /root/kimi-dash && python3 -m pytest tests/test_parse.py -k "kimi_code" -v`
Expected: FAIL — `_model_for()` missing a positional argument at the kimi-code call site.

- [ ] **Step 3: Wire the call site**

In `backend/parse.py`, inside `_parse_kimi_code`'s `if typ == "usage.record":` branch, replace:

```python
            model = _model_for(first_event_ts)
```

with:

```python
            model = _model_for(obj.get("model"), ts_dt or first_event_ts)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /root/kimi-dash && python3 -m pytest tests/test_parse.py -k "kimi_code" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd /root/kimi-dash
git add backend/parse.py tests/test_parse.py
git commit -m "fix(parse): honor the kimi-code wire model per record

k2.7-code is still selectable after the K3 cutoff, so a date can never
tell it from k3 — the wire can, and says so on every usage.record.
Fixes k2.7 billing at k3's ~3x rates, and k3 before the cutoff billing
at ~1/3. Also represents mid-session model switches.

Co-Authored-By: Kimi K2.6 <noreply@kimi.com>"
```

---

### Task 3: Wire the legacy path

**Files:**
- Modify: `backend/parse.py` (the `StatusUpdate` branch in `_parse_legacy`, around line 198)
- Test: `tests/test_parse.py`

**Interfaces:**
- Consumes: `parse._model_for(wire_model, ts)` from Task 1.
- Produces: nothing new.

Legacy transcripts carry no model string anywhere, so `wire_model` is always `None` here and the full date ladder applies. The only behavior change is per-record timestamps instead of the session's first event.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_parse.py`:

```python
def test_legacy_records_are_labelled_by_their_own_timestamp():
    """A legacy session that spans a cutoff must not stamp every record with
    the era of its first event. Legacy wires carry no model string, so the
    date ladder is all we have — but it applies per record.
    """
    blob = (
        _status_update_at(parse.K3_CUTOFF_EPOCH - 60)
        + _status_update_at(parse.K3_CUTOFF_EPOCH + 60)
    )
    out = parse.parse_file("sessions/projA/sess-span/wire.jsonl", blob)
    assert len(out["records"]) == 2
    assert [r["model"] for r in out["records"]] == ["kimi-k2-7-code", "kimi-k3"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /root/kimi-dash && python3 -m pytest tests/test_parse.py::test_legacy_records_are_labelled_by_their_own_timestamp -v`
Expected: FAIL — both records label `kimi-k2-7-code` (stamped from the first event), so the list comparison mismatches on the second element.

- [ ] **Step 3: Wire the call site**

In `backend/parse.py`, inside `_parse_legacy`'s StatusUpdate branch, replace the two-line comment and the call:

```python
            # Kimi wire format does not embed model per event; fall back to a
            # hardcoded time-based assignment using the session's first event.
            model = _model_for(first_event_ts)
```

with:

```python
            # Legacy wires embed no model string anywhere, so the date ladder
            # is the only available signal — applied to this record's own ts.
            model = _model_for(None, ts_dt or first_event_ts)
```

- [ ] **Step 4: Run the full suite**

Run: `cd /root/kimi-dash && python3 -m pytest tests/ -v`
Expected: PASS, all tests. `test_missing_timestamp_still_labels_k2_7_code` still passes via the `None` fallback; `test_at_k3_cutoff_labels_k3` and `test_one_second_before_k3_cutoff_still_labels_k2_7_code` reference `parse.K3_CUTOFF_EPOCH` symbolically, so the corrected constant does not break them.

- [ ] **Step 5: Commit**

```bash
cd /root/kimi-dash
git add backend/parse.py tests/test_parse.py
git commit -m "fix(parse): label legacy records by their own timestamp

A legacy session spanning a cutoff stamped every record with the era of
its first event, leaking records past their real boundary. Legacy wires
carry no model string, so dates remain the only signal — but per record.

Co-Authored-By: Kimi K2.6 <noreply@kimi.com>"
```

---

### Task 4: Mirror the ladder in the frontend parser

**Files:**
- Modify: `src/parser.js:17-42`

**Interfaces:**
- Consumes: the semantics fixed in Tasks 1-3.
- Produces: `modelForRecord(wireModel, ts)` replacing `modelForSession(firstEventTs)`.

`src/parser.js` has no test harness in this repo; correctness comes from mirroring `backend/parse.py` exactly. Read the final `_model_for` before writing this.

- [ ] **Step 1: Find every caller of the old function**

Run: `cd /root/kimi-dash && grep -rn "modelForSession" src/`
Expected: the definition plus its call sites. Every one must move to `modelForRecord` with the record's own model string and timestamp. If a call site has no wire model available, pass `null`.

- [ ] **Step 2: Replace the constants and the function**

In `src/parser.js`, replace the comment block, both constants, and `modelForSession` with:

```javascript
// Model attribution mirrors backend/parse.py — keep the two in lockstep.
//
// The wire is primary: a kimi-code usage.record carries the provider id on the
// record that becomes a billing row. Dates are the fallback, for what the wire
// cannot express: legacy transcripts carry no model string, and
// "kimi-for-coding" spans BOTH k2.6 and k2.7-code.
//
// Boundaries are strictly-before / inclusive-at: each cutoff instant belongs
// to the NEWER model.
const MODEL_CUTOFF_EPOCH = 1781217035;  // 2026-06-11 22:30:35 UTC
const K3_CUTOFF_EPOCH = 1784213155;     // 2026-07-16 14:45:55 UTC, earliest
                                        // observed k3 record. Only applies to
                                        // records with no wire model string.

const WIRE_MODEL_MAP = { k3: "kimi-k3" };
const AMBIGUOUS_WIRE_MODELS = new Set(["kimi-for-coding"]);

function wireModelSuffix(wireModel) {
  if (!wireModel) return null;
  const parts = wireModel.split("/");
  return parts[parts.length - 1];
}

// Raw provider id -> canonical pricing label, or null when the wire cannot
// settle it alone. Only canonical labels may reach rateForModel: it matches by
// substring, so a raw "kimi-code/k3" would match nothing and bill at
// DEFAULT_RATES (k2-6) — a ~3x undercount.
function canonicalModel(wireModel) {
  const suffix = wireModelSuffix(wireModel);
  if (!suffix) return null;
  return WIRE_MODEL_MAP[suffix] || null;
}

// Mirrors backend/parse.py _model_for, including the no-timestamp fallback to
// kimi-k2-7-code (an unstamped record is already-ingested history, so it
// cannot postdate the K3 cutoff, and K3's rates are ~3x higher).
function modelForRecord(wireModel, ts) {
  const canonical = canonicalModel(wireModel);
  if (canonical !== null) return canonical;

  const epoch = tsToEpoch(ts);
  if (epoch == null) return "kimi-k2-7-code";
  if (epoch < MODEL_CUTOFF_EPOCH) return "kimi-k2-6";
  // An ambiguous id names a real, non-k3 model: the date only separates the
  // k2 generations, and must never promote it to k3.
  if (AMBIGUOUS_WIRE_MODELS.has(wireModelSuffix(wireModel))) {
    return "kimi-k2-7-code";
  }
  if (epoch < K3_CUTOFF_EPOCH) return "kimi-k2-7-code";
  return "kimi-k3";
}
```

- [ ] **Step 3: Update the call sites found in Step 1**

Pass each record's own model string and timestamp. A `usage.record` in the kimi-code format exposes them as `obj.model` and `obj.time`; a legacy `StatusUpdate` has no model, so pass `null` and the record's own timestamp.

- [ ] **Step 4: Verify no caller of the old name survives**

Run: `cd /root/kimi-dash && grep -rn "modelForSession" src/ ; echo "exit=$?"`
Expected: no matches, `exit=1`.

- [ ] **Step 5: Commit**

```bash
cd /root/kimi-dash
git add src/parser.js
git commit -m "fix(ui): mirror the wire-first per-record model ladder

Keeps src/parser.js in lockstep with backend/parse.py.

Co-Authored-By: Kimi K2.6 <noreply@kimi.com>"
```

---

### Task 5: Bump PARSER_VERSION and reparse

**Files:**
- Modify: `backend/.env.example:20`
- Modify: `src/parser.js:7`
- Modify: `.env` (untracked, live)

**Interfaces:**
- Consumes: Tasks 1-4 complete.
- Produces: reparsed `records` rows.

A parse-algorithm change must invalidate every `files` row or the DB keeps the old labels.

- [ ] **Step 1: Bump both tracked files**

In `backend/.env.example`: `PARSER_VERSION=5` → `PARSER_VERSION=6`
In `src/parser.js`: `window.PARSER_VERSION = "5";` → `window.PARSER_VERSION = "6";`

- [ ] **Step 2: Verify they agree**

Run: `cd /root/kimi-dash && grep -rn "PARSER_VERSION" backend/.env.example src/parser.js`
Expected: both read `6`.

- [ ] **Step 3: Run the full suite**

Run: `cd /root/kimi-dash && python3 -m pytest tests/ -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
cd /root/kimi-dash
git add backend/.env.example src/parser.js
git commit -m "chore: bump PARSER_VERSION to 6

Model attribution is now wire-first and per-record; every files row must
reparse to pick up corrected labels.

Co-Authored-By: Kimi K2.6 <noreply@kimi.com>"
```

- [ ] **Step 5: Bump the live .env and reparse**

```bash
cd /root/kimi-dash
sed -i 's/^PARSER_VERSION=5$/PARSER_VERSION=6/' .env
grep '^PARSER_VERSION' .env
```
Expected: `PARSER_VERSION=6`.

Then trigger the reparse the way this project already does it — do NOT hand-roll a
reparse script. Check `backend/ingest.py` for the entry point and use it.

- [ ] **Step 6: Verify against the four spec checks**

```bash
cd /root/kimi-dash
psql "$(grep DATABASE_URL_VIZ .env | cut -d= -f2-)" -c "
SELECT model, count(*) AS recs, min(ts)::timestamptz(0) AS first,
       max(ts)::timestamptz(0) AS last
FROM records GROUP BY model ORDER BY model;"
```

Expected, and each must be checked explicitly:
1. No `kimi-k2-7-code` record later than `2026-07-16 15:06:34` **except** ones whose wire says `kimi-for-coding` — post-cutoff k2.7 is now correct, not a bug. Report what you find rather than forcing a number.
2. `kimi-k3` first_ts moves earlier — to `2026-07-16 14:45:55`, from `15:14:56`.
3. The 16 legacy files dated `18:16`–`18:25` still label `kimi-k3`.
4. Total row count is unchanged; only labels and costs move.

Report the before/after table in your final message. The before is:

```
 kimi-k2-6      |   16932 | 2026-05-12 00:47:03+00 | 2026-06-10 22:42:03+00 | 303.60
 kimi-k2-7-code |    8736 | 2026-06-12 15:23:14+00 | 2026-07-16 15:10:13+00 | 145.56
 kimi-k3        |     336 | 2026-07-16 15:14:55+00 | 2026-07-16 18:25:47+00 |   9.52
```

---

## Self-Review

**Spec coverage:** ladder → Task 1; corrected `K3_CUTOFF_EPOCH` → Task 1; kimi-code call site → Task 2; legacy call site → Task 3; `parser.js` mirror → Task 4; `PARSER_VERSION` + reparse → Task 5; the one inverted test → Task 2; the two renamed tests → Task 2; all seven new test cases → Tasks 1-3; the `rate_for` substring trap → Task 2 Step 1 (explicit `!=` assertion against k2-6 rates). `pricing.py` needs no edit, per the spec.

**Type consistency:** `_model_for(wire_model, ts)` and `_canonical_model(wire_model)` are used with that exact arity and order in Tasks 1-3. JS mirrors are `modelForRecord(wireModel, ts)` / `canonicalModel(wireModel)`, consistent within Task 4.

**Known wrinkle, deliberately handled:** `_canonical_model` returns `None` for both "ambiguous" and "unrecognized". `_model_for` therefore re-checks `_AMBIGUOUS_WIRE_MODELS` to stop `kimi-for-coding` from being promoted to k3 by a late date, while still letting a genuinely unknown id fall through the full ladder. Task 1 Step 3 and Task 4 Step 2 both implement this; the `test_model_for_kimi_for_coding_never_becomes_k3` case pins it.
