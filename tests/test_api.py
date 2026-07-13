import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _build_app(monkeypatch, pre_ingest=None, test_db="kimi_viz_test_api"):
    """Spin up a fresh DB + mini R2, optionally mutate the temp R2 tree,
    ingest, and return a TestClient plus cleanup metadata.

    Bypasses auth via a clean FastAPI app with only the api router.
    """
    os.system(f"dropdb --if-exists {test_db} 2>/dev/null")
    os.system(f"createdb {test_db} 2>/dev/null")
    os.system(f"psql {test_db} -f {_REPO_ROOT / 'backend/schema.sql'} >/dev/null")
    monkeypatch.setenv("DATABASE_URL_VIZ", f"postgresql:///{test_db}")
    src = _REPO_ROOT / "fixtures/r2_mini"
    tmp = tempfile.mkdtemp(prefix="kd-api-")
    shutil.copytree(src, Path(tmp) / "r2")
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp}/r2/")

    if pre_ingest is not None:
        pre_ingest(Path(tmp) / "r2")

    from backend import db as _db
    if _db._VIZ is not None:
        try:
            _db._VIZ.close()
        except Exception:
            pass
    _db._VIZ = None

    from backend import ingest
    ingest.run_ingest(trigger="manual")

    from fastapi import FastAPI
    from backend import api as api_mod
    a = FastAPI()
    a.include_router(api_mod.router)

    return TestClient(a), tmp, test_db


def _cleanup_app(tmp, test_db):
    from backend import db as _db
    if _db._VIZ is not None:
        try:
            _db._VIZ.close()
        except Exception:
            pass
    _db._VIZ = None
    shutil.rmtree(tmp)
    os.system(f"dropdb --if-exists {test_db} 2>/dev/null")


@pytest.fixture
def app_with_data(monkeypatch):
    client, tmp, test_db = _build_app(monkeypatch)
    yield client
    _cleanup_app(tmp, test_db)


@pytest.fixture
def app_with_unresolved(monkeypatch):
    """Plain fixture plus two junk hash-projects (no project.json marker).

    One 32-hex legacy md5 id and one 12-hex kimi-code workdir hash id,
    each with a distinct session dir so session_count sees 2.
    """
    def _inject_junk(r2_root: Path):
        src_wire = (
            _REPO_ROOT
            / "fixtures/r2_mini/kimi/sessions/projB/sess-C/subagents/agent-aaaa/wire.jsonl"
        )
        template = src_wire.read_text()
        junk = [
            ("0123456789abcdef0123456789abcdef", "test1", "unresolved-uuid-32"),
            ("abcdef012345", "test2", "unresolved-uuid-12"),
        ]
        for project_id, session_id, uuid in junk:
            wire = template.replace('"shared-uuid-1"', f'"{uuid}"')
            dest = (
                r2_root
                / "kimi/sessions"
                / project_id
                / session_id
                / "subagents/agent-x/wire.jsonl"
            )
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(wire)

    client, tmp, test_db = _build_app(
        monkeypatch, pre_ingest=_inject_junk, test_db="kimi_viz_test_unres"
    )
    yield client
    _cleanup_app(tmp, test_db)


def test_projects(app_with_data):
    r = app_with_data.get("/api/projects")
    assert r.status_code == 200
    body = r.json()
    pids = sorted(p["project_id"] for p in body["projects"])
    assert pids == ["projA", "projB"]
    for p in body["projects"]:
        assert "file_count" in p and "total_cost" in p


def test_cache_per_model_shape(app_with_data):
    r = app_with_data.get("/api/cache?range=3650d")
    assert r.status_code == 200
    body = r.json()
    assert "per_model" in body and "session_total" in body
    assert "top_output" in body and "top_cache_read" in body
    if body["per_model"]:
        m = body["per_model"][0]
        assert {"model", "turns", "fresh", "cache_read", "output",
                "hit_rate_pct", "cost_total", "cost_buckets"} <= set(m)
        assert {"fresh", "read", "output"} == set(m["cost_buckets"])


def test_cache_dedups_cross_file_uuid(app_with_data):
    """sess-C main + subagent peer + sess-D main all have uuid='shared-uuid-1'.
    Records table holds 3 rows for that uuid; DISTINCT ON dedups to 1 in the
    per_model totals.

    sess-C main has input=1000, output=500 (single record).
    sess-C subagent has input=1000, output=500 (same uuid -> dedup'd).
    sess-D main has 2 records: shared-uuid-1 (1000/500, dedup'd) +
                                sess-D-only (50/25, kept).

    After cross-file dedup:
      shared-uuid-1 winner = lexicographically-first file_key, which is
      sessions/projB/sess-C/subagents/agent-aaaa/wire.jsonl.
      One row claims the shared uuid; the other two drop. The remaining tally
      for projB: 1000 + 50 input, 500 + 25 output.
    """
    r = app_with_data.get("/api/cache?range=3650d&project=projB")
    body = r.json()
    assert body["session_total"]["fresh"] == 1050
    assert body["session_total"]["output"] == 525
    assert body["session_total"]["turns"] == 2


def test_cache_top_n_limited_to_10(app_with_data):
    r = app_with_data.get("/api/cache?range=3650d")
    body = r.json()
    assert len(body["top_output"]) <= 10
    assert len(body["top_cache_read"]) <= 10


def test_cache_bad_range_400(app_with_data):
    r = app_with_data.get("/api/cache?range=abc")
    assert r.status_code == 400


def test_cache_session_total_matches_per_model_sum(app_with_data):
    r = app_with_data.get("/api/cache?range=3650d")
    body = r.json()
    sum_turns = sum(m["turns"] for m in body["per_model"])
    sum_cost = round(sum(m["cost_total"] for m in body["per_model"]), 4)
    assert body["session_total"]["turns"] == sum_turns
    assert body["session_total"]["cost_total"] == sum_cost


def test_transcript_streams(app_with_data):
    r = app_with_data.get("/api/sessions/sess-A/transcript")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/x-ndjson"
    import json
    first = r.text.split("\n")[0]
    obj = json.loads(first)
    # Kimi wire.jsonl puts the event type under message.type.
    assert "message" in obj and "type" in obj["message"]


def test_transcript_etag_header(app_with_data):
    r = app_with_data.get("/api/sessions/sess-A/transcript")
    assert "etag" in {k.lower() for k in r.headers.keys()}


def test_transcript_404(app_with_data):
    r = app_with_data.get("/api/sessions/does-not-exist/transcript")
    assert r.status_code == 404


def test_sidecar_path_validation(app_with_data):
    r = app_with_data.get(
        "/api/sessions/sess-A/sidecar",
        params={"path": "data/tool-results/x.txt"},
    )
    assert r.status_code == 200
    assert r.text.strip() == "tool output"
    r2 = app_with_data.get(
        "/api/sessions/sess-A/sidecar",
        params={"path": "../../../etc/passwd"},
    )
    assert r2.status_code == 400


def test_sidecar_absolute_path_rejected(app_with_data):
    r = app_with_data.get(
        "/api/sessions/sess-A/sidecar",
        params={"path": "/etc/passwd"},
    )
    assert r.status_code == 400


def test_sidecar_missing_file_404(app_with_data):
    r = app_with_data.get(
        "/api/sessions/sess-A/sidecar",
        params={"path": "data/does-not-exist.txt"},
    )
    assert r.status_code == 404


def test_context_growth_agg_shape(app_with_data):
    r = app_with_data.get("/api/context-growth/agg?range=3650d")
    assert r.status_code == 200
    body = r.json()
    assert "per_turn" in body and "per_session_final" in body
    for k in ("n", "mean", "p50", "p90", "p99", "max"):
        assert k in body["per_turn"]
        assert k in body["per_session_final"]


def test_context_growth_session_returns_canonical_array(app_with_data):
    """Mini fixture sess-A has 1 turn (single TurnBegin->StatusUpdate->TurnEnd).
    Verify the per-turn array is returned with the canonical shape."""
    r = app_with_data.get("/api/context-growth/session/sess-A")
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == "sess-A"
    assert "turns" in body and isinstance(body["turns"], list)
    if body["turns"]:
        t = body["turns"][0]
        assert {"idx", "ts", "line", "input", "output", "delta"} == set(t)
    assert body["total_turns"] == len(body["turns"])


def test_context_growth_session_404(app_with_data):
    r = app_with_data.get("/api/context-growth/session/does-not-exist")
    assert r.status_code == 404


def test_tool_error_rate_returns_expected_shape(app_with_data):
    r = app_with_data.get("/api/tool-error-rate?range=3650d")
    assert r.status_code == 200
    body = r.json()
    assert "range" in body
    assert "bucket_s" in body
    assert "buckets" in body
    assert isinstance(body["buckets"], list)
    for b in body["buckets"]:
        assert {"ts", "model", "tool", "n_total", "n_error"} <= set(b.keys())
        assert b["n_error"] <= b["n_total"]


def test_projects_groups_unresolved(app_with_unresolved):
    r = app_with_unresolved.get("/api/projects")
    assert r.status_code == 200
    body = r.json()
    pids = sorted(p["project_id"] for p in body["projects"])
    assert pids == ["<unresolved>", "projA", "projB"]
    unresolved = next(p for p in body["projects"] if p["project_id"] == "<unresolved>")
    assert unresolved["display_name"] == "<unresolved>"
    assert unresolved["session_count"] == 2


def test_unresolved_filter_scopes_queries(app_with_unresolved):
    r_all = app_with_unresolved.get("/api/cache?range=3650d")
    assert r_all.status_code == 200
    all_total = r_all.json()["session_total"]

    r_unres = app_with_unresolved.get(
        "/api/cache", params={"range": "3650d", "project": "<unresolved>"}
    )
    assert r_unres.status_code == 200
    unres_total = r_unres.json()["session_total"]
    assert unres_total["turns"] > 0
    assert unres_total["cost_total"] > 0
    assert unres_total["turns"] < all_total["turns"]
    assert unres_total["cost_total"] < all_total["cost_total"]

    # Junk hash-projects must not leak into a normal project's filtered numbers.
    # These are the documented projB totals under the plain fixture.
    r_projB = app_with_unresolved.get(
        "/api/cache", params={"range": "3650d", "project": "projB"}
    )
    projB_total = r_projB.json()["session_total"]
    assert projB_total["turns"] == 2
    assert projB_total["fresh"] == 1050
    assert projB_total["output"] == 525
    assert projB_total["cost_total"] == 0.0031


def test_projects_unaffected_without_junk(app_with_data):
    r = app_with_data.get("/api/projects")
    assert r.status_code == 200
    pids = [p["project_id"] for p in r.json()["projects"]]
    assert "<unresolved>" not in pids


# ---------------------------------------------------------------- heatmap

def _insert_tz_probe_rows():
    """Two records with a unique model, one in winter (CET, UTC+1) and one
    in summer (CEST, UTC+2), to prove the endpoint is DST-aware."""
    import psycopg
    with psycopg.connect(os.environ["DATABASE_URL_VIZ"]) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO projects (project_id, display_name, first_seen_at, last_seen_at) "
            "VALUES ('projTZ', 'projTZ', now(), now()) ON CONFLICT DO NOTHING"
        )
        cur.execute(
            "INSERT INTO files (file_key, project_id, session_id, is_main, r2_etag, "
            "r2_size_bytes, r2_last_modified, parsed_at, parser_version) "
            "VALUES ('projTZ/tz.jsonl', 'projTZ', 'tzsess', TRUE, 'etag-tz', 1, now(), now(), 'test')"
        )
        cur.execute(
            "INSERT INTO records (file_key, line_num, uuid, ts, model, output_tokens, cost_usd) VALUES "
            # 2026-01-15 is a Thursday (ISODOW 4); 10:30Z in CET (UTC+1) is 11:30 local.
            "('projTZ/tz.jsonl', 1, 'uuid-tz-winter', '2026-01-15T10:30:00Z', 'tz-probe-model', 10, 0.01), "
            # 2026-07-15 is a Wednesday (ISODOW 3); 10:30Z in CEST (UTC+2) is 12:30 local.
            "('projTZ/tz.jsonl', 2, 'uuid-tz-summer', '2026-07-15T10:30:00Z', 'tz-probe-model', 20, 0.02)"
        )
        conn.commit()


def test_activity_heatmap_shape(app_with_data):
    r = app_with_data.get("/api/activity-heatmap?range=3650d")
    assert r.status_code == 200
    body = r.json()
    assert body["tz"] == "Europe/Prague"
    assert body["cells"], "mini fixture must produce at least one cell"
    for c in body["cells"]:
        assert 1 <= c["dow"] <= 7
        assert 0 <= c["hour"] <= 23
        assert c["requests"] >= 1
        assert c["output_tokens"] >= 0
        assert c["cost_usd"] >= 0


def test_activity_heatmap_requests_match_dashboard(app_with_data):
    # Both endpoints read through the same DISTINCT ON (uuid) dedup, so
    # total request counts must agree for the same range.
    heat = app_with_data.get("/api/activity-heatmap?range=3650d").json()
    dash = app_with_data.get("/api/dashboard?range=3650d").json()
    assert sum(c["requests"] for c in heat["cells"]) == \
           sum(h["requests"] for h in dash["hourly"])


def test_activity_heatmap_dst_awareness(app_with_data):
    _insert_tz_probe_rows()
    r = app_with_data.get("/api/activity-heatmap?range=3650d&model=tz-probe-model")
    assert r.status_code == 200
    cells = {(c["dow"], c["hour"]): c for c in r.json()["cells"]}
    assert set(cells) == {(4, 11), (3, 12)}, cells
    assert cells[(4, 11)]["requests"] == 1   # winter: 10:30Z -> 11:30 CET, Thu
    assert cells[(3, 12)]["requests"] == 1   # summer: 10:30Z -> 12:30 CEST, Wed
    assert cells[(3, 12)]["output_tokens"] == 20


def test_activity_heatmap_project_filter(app_with_data):
    both = app_with_data.get("/api/activity-heatmap?range=3650d").json()
    one = app_with_data.get("/api/activity-heatmap?range=3650d&project=projA").json()
    assert sum(c["requests"] for c in one["cells"]) < \
           sum(c["requests"] for c in both["cells"])


def test_activity_heatmap_bad_range_400(app_with_data):
    assert app_with_data.get("/api/activity-heatmap?range=bogus").status_code == 400
