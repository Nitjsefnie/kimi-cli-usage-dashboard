"""R2 → Postgres ingest.

Per-file granularity: every wire.jsonl under bucket root → one row in `files`
+ N rows in `records` (per StatusUpdate). Cross-file uuid dedup is a
query-time concern.

Reparse trigger per FILE: row missing OR etag changed OR parser_version
mismatch. Orphan files (R2 key gone) are deleted. CASCADE drops records.

Kimi R2 layout:
  sessions/{session_hash}/{uuid}/wire.jsonl
  sessions/{session_hash}/{uuid}/context.jsonl
  sessions/{session_hash}/{uuid}/state.json

We ingest only wire.jsonl; project_id = session_hash, session_id = uuid.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from backend import db, events, parse, r2


def run_ingest(trigger: str) -> dict:
    started = datetime.now(timezone.utc)
    parser_version = os.environ.get("PARSER_VERSION", "1")

    with db.viz_conn() as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO ingest_runs (started_at, trigger) VALUES (%s, %s) "
            "RETURNING id",
            (started, trigger),
        )
        run_id = cur.fetchone()[0]
        c.commit()

    listed = 0
    inserted = 0
    reparsed = 0
    deleted = 0
    err = None

    try:
        # Cache existing file_key → (etag, parser_version) for reparse decisions
        with db.viz_conn() as c:
            existing = {
                row[0]: (row[1], row[2])
                for row in c.execute(
                    "SELECT file_key, r2_etag, parser_version FROM files"
                ).fetchall()
            }

        # Pre-walk R2 once to (a) collect wire.jsonl keys and (b) fetch every
        # sessions/<HASH>/project.json marker. The marker is published by
        # archive_sessions.py on the originating box and carries the path
        # the session was run from — we use it as the project's display_name.
        wire_objs: list = []
        project_paths: dict[str, str] = {}
        for obj in r2.list_keys():
            parts = obj.key.split("/")
            if len(parts) >= 4 and parts[0] == "sessions" \
                    and parts[-1] == "wire.jsonl" and obj.key.endswith(".jsonl"):
                wire_objs.append(obj)
            elif len(parts) == 3 and parts[0] == "sessions" \
                    and parts[2] == "project.json":
                try:
                    blob = r2.get_object(obj.key)
                    data = json.loads(blob.decode("utf-8"))
                    path = data.get("path")
                    if isinstance(path, str) and path:
                        project_paths[parts[1]] = path
                except (OSError, ValueError, KeyError, json.JSONDecodeError):
                    pass

        seen_keys: set[str] = set()
        seen_projects: dict[str, dict] = {}

        for obj in wire_objs:
            parts = obj.key.split("/")
            project_id = parts[1]
            session_id = parts[2]
            # Subagent transcripts live at .../subagents/<sub-id>/wire.jsonl;
            # main session transcripts at .../<uuid>/wire.jsonl. Both get
            # ingested, but the classification drives the main/subagent
            # split surfaced by /api/dashboard.
            is_main = "/subagents/" not in obj.key
            listed += 1
            seen_keys.add(obj.key)

            proj = seen_projects.setdefault(project_id, {
                "project_id": project_id,
                "display_name": project_paths.get(project_id, project_id),
                "first_seen_at": obj.last_modified,
                "last_seen_at": obj.last_modified,
            })
            if obj.last_modified < proj["first_seen_at"]:
                proj["first_seen_at"] = obj.last_modified
            if obj.last_modified > proj["last_seen_at"]:
                proj["last_seen_at"] = obj.last_modified

            stored = existing.get(obj.key)
            need_reparse = (
                stored is None
                or stored[0] != obj.etag
                or stored[1] != parser_version
            )
            if not need_reparse:
                continue

            blob = r2.get_object(obj.key)
            parsed = parse.parse_file(obj.key, blob)

            with db.viz_conn() as c, c.cursor() as cur:
                cur.execute(
                    "INSERT INTO projects (project_id, display_name, "
                    "first_seen_at, last_seen_at) "
                    "VALUES (%(project_id)s, %(display_name)s, "
                    "%(first_seen_at)s, %(last_seen_at)s) "
                    "ON CONFLICT (project_id) DO UPDATE SET "
                    "  display_name = EXCLUDED.display_name, "
                    "  first_seen_at = LEAST(projects.first_seen_at, "
                    "                        EXCLUDED.first_seen_at), "
                    "  last_seen_at = GREATEST(projects.last_seen_at, "
                    "                          EXCLUDED.last_seen_at)",
                    proj,
                )
                cur.execute(
                    "DELETE FROM records WHERE file_key = %s", (obj.key,)
                )
                cur.execute(
                    """
                    INSERT INTO files (file_key, project_id, session_id,
                      is_main, r2_etag, r2_size_bytes, r2_last_modified,
                      parsed_at, parser_version, ctx_turns, turn_count,
                      rate_limit_hits)
                    VALUES (%(file_key)s, %(project_id)s, %(session_id)s,
                      %(is_main)s, %(r2_etag)s, %(r2_size_bytes)s,
                      %(r2_last_modified)s, %(parsed_at)s, %(parser_version)s,
                      %(ctx_turns)s::jsonb, %(turn_count)s,
                      %(rate_limit_hits)s::jsonb)
                    ON CONFLICT (file_key) DO UPDATE SET
                      project_id = EXCLUDED.project_id,
                      session_id = EXCLUDED.session_id,
                      is_main = EXCLUDED.is_main,
                      r2_etag = EXCLUDED.r2_etag,
                      r2_size_bytes = EXCLUDED.r2_size_bytes,
                      r2_last_modified = EXCLUDED.r2_last_modified,
                      parsed_at = EXCLUDED.parsed_at,
                      parser_version = EXCLUDED.parser_version,
                      ctx_turns = EXCLUDED.ctx_turns,
                      turn_count = EXCLUDED.turn_count,
                      rate_limit_hits = EXCLUDED.rate_limit_hits
                    """,
                    {
                        "file_key": obj.key,
                        "project_id": project_id,
                        "session_id": session_id,
                        "is_main": is_main,
                        "r2_etag": obj.etag,
                        "r2_size_bytes": obj.size,
                        "r2_last_modified": obj.last_modified,
                        "parsed_at": datetime.now(timezone.utc),
                        "parser_version": parser_version,
                        "ctx_turns": json.dumps(parsed["ctx_turns"], default=str),
                        "turn_count": parsed["turn_count"],
                        "rate_limit_hits": json.dumps(
                            parsed.get("rate_limit_hits", []), default=str
                        ),
                    },
                )
                cur.execute(
                    "DELETE FROM tool_uses WHERE file_key = %s", (obj.key,)
                )
                if parsed.get("tool_uses"):
                    cur.executemany(
                        """
                        INSERT INTO tool_uses (file_key, line_num, idx, ts, tool_name, is_error)
                        VALUES (%(file_key)s, %(line_num)s, %(idx)s, %(ts)s, %(tool_name)s, %(is_error)s)
                        """,
                        parsed["tool_uses"],
                    )
                if parsed["records"]:
                    cur.executemany(
                        """
                        INSERT INTO records (file_key, line_num, uuid,
                          ts, model, fresh_tokens,
                          cache_creation_tokens, cache_read_tokens,
                          output_tokens, cost_usd,
                          text_chars, reply_latency_s)
                        VALUES (%(file_key)s, %(line_num)s, %(uuid)s,
                          %(ts)s, %(model)s,
                          %(fresh_tokens)s, %(cache_creation_tokens)s,
                          %(cache_read_tokens)s, %(output_tokens)s,
                          %(cost_usd)s,
                          %(text_chars)s, %(reply_latency_s)s)
                        """,
                        parsed["records"],
                    )
                c.commit()
            if stored is None:
                inserted += 1
            else:
                reparsed += 1

        # Unconditional project upsert — even when no files needed reparse,
        # the project marker's path may have changed (e.g. user renamed a
        # work_dir), and the per-file inner block above runs only on reparse.
        # Pushing every seen project here keeps display_name fresh.
        if seen_projects:
            with db.viz_conn() as c, c.cursor() as cur:
                cur.executemany(
                    "INSERT INTO projects (project_id, display_name, "
                    "first_seen_at, last_seen_at) "
                    "VALUES (%(project_id)s, %(display_name)s, "
                    "%(first_seen_at)s, %(last_seen_at)s) "
                    "ON CONFLICT (project_id) DO UPDATE SET "
                    "  display_name = EXCLUDED.display_name, "
                    "  first_seen_at = LEAST(projects.first_seen_at, "
                    "                        EXCLUDED.first_seen_at), "
                    "  last_seen_at = GREATEST(projects.last_seen_at, "
                    "                          EXCLUDED.last_seen_at)",
                    list(seen_projects.values()),
                )
                c.commit()

        # Orphan files
        with db.viz_conn() as c, c.cursor() as cur:
            if seen_keys:
                cur.execute(
                    "DELETE FROM files WHERE file_key != ALL(%s) RETURNING 1",
                    (list(seen_keys),),
                )
            else:
                cur.execute("DELETE FROM files RETURNING 1")
            deleted = len(cur.fetchall())
            c.commit()

    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"

    finished = datetime.now(timezone.utc)
    with db.viz_conn() as c, c.cursor() as cur:
        cur.execute(
            "UPDATE ingest_runs SET finished_at=%s, r2_listed=%s, "
            "reparsed=%s, inserted=%s, deleted=%s, error=%s WHERE id=%s",
            (finished, listed, reparsed, inserted, deleted, err, run_id),
        )
        c.commit()

    summary = {
        "id": run_id,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "trigger": trigger,
        "r2_listed": listed,
        "inserted": inserted,
        "reparsed": reparsed,
        "deleted": deleted,
        "error": err,
    }
    if err is None and (inserted or reparsed or deleted):
        events.broadcast_threadsafe("ingest_done", summary)

    return summary
