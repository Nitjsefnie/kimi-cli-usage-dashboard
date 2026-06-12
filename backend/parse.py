"""wire.jsonl → per-file (records list + ctx_turns array).

Each call to parse_file processes ONE wire.jsonl. Cross-file uuid dedup is a
query-time concern (DISTINCT ON (uuid) in the read endpoints).

Cost is precomputed per-record using pricing.MODEL_RATES so the
read path doesn't need to JOIN against rates. Bumps to the rate
table OR to the parse algorithm both require a PARSER_VERSION
bump to invalidate every files row.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

import orjson

from backend import pricing

# Hardcoded model transition.  The anchor is "now - 12h"; the effective
# cutoff is "anchor - 12h".  Sessions whose first event is strictly
# before this UTC epoch are labelled kimi-k2-6, everything else (including
# sessions with no usable timestamp) is labelled kimi-k2-7-code.
MODEL_CUTOFF_EPOCH = 1781217035
MODEL_CUTOFF_DT = datetime.fromtimestamp(MODEL_CUTOFF_EPOCH, tz=timezone.utc)


def _to_dt(s: str | float | None):
    if not s:
        return None
    if isinstance(s, (int, float)):
        return datetime.fromtimestamp(s, tz=datetime.now().astimezone().tzinfo)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def parse_file(file_key: str, blob: bytes) -> dict:
    """Parse one wire.jsonl. Returns {records, ctx_turns, turn_count, rate_limit_hits, tool_uses}.

    records: list of dicts with keys
      file_key, line_num, uuid, ts, model,
      fresh_tokens, cache_creation_tokens, cache_read_tokens,
      output_tokens, text_chars, reply_latency_s, cost_usd

    ctx_turns: list of dicts with keys
      idx, ts, line, input, output, delta
    """
    records_in_order: list[dict] = []
    tool_uses: list[dict] = []
    rate_limit_hits: list[dict] = []

    # Per-file map of tool_call_id -> bool(is_error)
    tool_result_is_error: dict[str, bool] = {}

    # Turn tracking
    turns: list[dict] = []  # {begin_line, begin_ts, end_line, end_ts, status_lines: [line_num]}
    current_turn: dict | None = None

    # For reply latency: track TurnBegin ts, then find first assistant event
    pending_turn_begin_ts: datetime | None = None
    pending_turn_begin_line: int = 0
    turn_has_assistant_event: bool = False

    # For text_chars: accumulate ContentPart.text since last TurnBegin
    text_chars_since_turn: int = 0

    # First event timestamp drives the per-session model label.
    first_event_ts: datetime | None = None

    for line_num, raw in enumerate(blob.splitlines(), 1):
        if not raw:
            continue
        try:
            obj = orjson.loads(raw)
        except orjson.JSONDecodeError:
            continue

        ts_raw = obj.get("timestamp")
        msg = obj.get("message") or {}
        msg_type = msg.get("type", "")
        payload = msg.get("payload", {})
        ts_dt = _to_dt(ts_raw)
        if ts_dt is not None and first_event_ts is None:
            first_event_ts = ts_dt

        if msg_type == "TurnBegin":
            # Close previous turn if open
            if current_turn is not None:
                turns.append(current_turn)
            current_turn = {
                "begin_line": line_num,
                "begin_ts": ts_dt,
                "end_line": None,
                "end_ts": None,
                "status_lines": [],
            }
            pending_turn_begin_ts = ts_dt
            pending_turn_begin_line = line_num
            turn_has_assistant_event = False
            text_chars_since_turn = 0
            continue

        if msg_type == "TurnEnd" and current_turn is not None:
            current_turn["end_line"] = line_num
            current_turn["end_ts"] = ts_dt
            turns.append(current_turn)
            current_turn = None
            pending_turn_begin_ts = None
            continue

        if msg_type == "ContentPart":
            part_type = payload.get("type", "")
            if part_type == "text":
                text_chars_since_turn += len(str(payload.get("text", "")))
            # Any ContentPart or ToolCall counts as an assistant event
            if not turn_has_assistant_event and pending_turn_begin_ts is not None:
                turn_has_assistant_event = True
            continue

        if msg_type == "ToolCall":
            func = payload.get("function", {})
            tool_name = func.get("name", "")
            tool_call_id = payload.get("id", "")
            tool_uses.append({
                "file_key": file_key,
                "line_num": line_num,
                "idx": len(tool_uses),
                "ts": ts_dt,
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "is_error": None,
            })
            if not turn_has_assistant_event and pending_turn_begin_ts is not None:
                turn_has_assistant_event = True
            continue

        if msg_type == "ToolResult":
            return_value = payload.get("return_value", {})
            tc_id = payload.get("tool_call_id", "")
            is_err = False
            if isinstance(return_value, dict):
                is_err = bool(return_value.get("is_error", False))
            if tc_id:
                tool_result_is_error[str(tc_id)] = is_err
            continue

        if msg_type == "StatusUpdate":
            tu = payload.get("token_usage")
            if not tu:
                continue

            fresh = int(tu.get("input_other", 0) or 0)
            create = int(tu.get("input_cache_creation", 0) or 0)
            read = int(tu.get("input_cache_read", 0) or 0)
            output = int(tu.get("output", 0) or 0)
            total_input = fresh + create + read

            # Reply latency: gap from TurnBegin to first assistant event
            reply_latency_s: float | None = None
            if pending_turn_begin_ts is not None and ts_dt is not None:
                delta_s = (ts_dt - pending_turn_begin_ts).total_seconds()
                if delta_s >= 0:
                    reply_latency_s = delta_s
            # Consume the anchor once we record a StatusUpdate for this turn
            pending_turn_begin_ts = None

            # Kimi wire format does not embed model per event; fall back to a
            # hardcoded time-based assignment using the session's first event.
            if first_event_ts is not None and first_event_ts < MODEL_CUTOFF_DT:
                model = "kimi-k2-6"
            else:
                model = "kimi-k2-7-code"
            cost = pricing.compute_cost(
                model,
                fresh=fresh, create=create, read=read, output=output,
            )

            records_in_order.append({
                "file_key": file_key,
                "line_num": line_num,
                "uuid": payload.get("message_id") or None,
                "ts": ts_dt,
                "model": model,
                "fresh_tokens": fresh,
                "cache_creation_tokens": create,
                "cache_read_tokens": read,
                "output_tokens": output,
                "cost_usd": round(cost, 6),
                "text_chars": text_chars_since_turn,
                "reply_latency_s": reply_latency_s,
                "ctx_input": total_input,
            })

            if current_turn is not None:
                current_turn["status_lines"].append(line_num)
            continue

    # Close dangling turn
    if current_turn is not None:
        turns.append(current_turn)

    # Resolve tool_result.is_error onto each tool_uses entry
    for tu in tool_uses:
        tc_id = tu.pop("tool_call_id", "")
        if tc_id and tc_id in tool_result_is_error:
            tu["is_error"] = tool_result_is_error[tc_id]

    # Build ctx_turns from turns + records
    # Map line_num -> record for quick lookup
    rec_by_line = {r["line_num"]: r for r in records_in_order}

    ctx_turns: list[dict] = []
    prev_input = 0
    turn_idx = 0
    for turn in turns:
        # Use the last StatusUpdate in this turn as the canonical one
        if not turn["status_lines"]:
            continue
        last_line = turn["status_lines"][-1]
        rec = rec_by_line.get(last_line)
        if not rec or rec["ctx_input"] <= 0:
            continue
        turn_idx += 1
        ctx_input = rec["ctx_input"]
        ctx_turns.append({
            "idx": turn_idx,
            "ts": rec["ts"].isoformat() if rec["ts"] else "",
            "line": last_line,
            "input": ctx_input,
            "output": rec["output_tokens"],
            "delta": ctx_input - prev_input,
        })
        prev_input = ctx_input

    return {
        "records": records_in_order,
        "ctx_turns": ctx_turns,
        "turn_count": len(ctx_turns),
        "rate_limit_hits": rate_limit_hits,
        "tool_uses": tool_uses,
    }
