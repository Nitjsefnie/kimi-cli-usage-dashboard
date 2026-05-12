/**
 * In-browser wire.jsonl parser for the Inspector.
 * Mirrors backend/parse.py and ~/.kimi/scripts/parse_wire.py.
 */

window.PARSER_VERSION = "1";

const MODEL_RATES = {
  "kimi-for-coding": { fresh: 0.95, create: 0.00, read: 0.16, output: 4.00 },
  "kimi-k2-6":       { fresh: 0.95, create: 0.00, read: 0.16, output: 4.00 },
  "kimi-k2":         { fresh: 0.95, create: 0.00, read: 0.16, output: 4.00 },
  "kimi":            { fresh: 0.95, create: 0.00, read: 0.16, output: 4.00 },
};

const DEFAULT_RATES = MODEL_RATES["kimi-k2-6"];

window.rateForModel = function rateForModel(model) {
  if (!model) return DEFAULT_RATES;
  for (const key in MODEL_RATES) {
    if (model.includes(key)) {
      const r = MODEL_RATES[key];
      return {
        fresh: r.fresh, create: r.create, read: r.read, output: r.output,
        out: r.output, c5: r.create, c1h: 0,
      };
    }
  }
  const r = DEFAULT_RATES;
  return {
    fresh: r.fresh, create: r.create, read: r.read, output: r.output,
    out: r.output, c5: r.create, c1h: 0,
  };
};

function parseTimestamp(ts) {
  if (!ts) return null;
  if (typeof ts === "number") return new Date(ts * 1000);
  const d = new Date(ts);
  return isNaN(d.getTime()) ? null : d;
}

function shortTime(ts) {
  const dt = parseTimestamp(ts);
  if (!dt) return "????-??-?? ??:??:??";
  return dt.toISOString().replace("T", " ").slice(0, 19);
}

function _iterJsonObjects(line) {
  const out = [];
  let pos = 0;
  const n = line.length;
  while (pos < n) {
    while (pos < n && /\s/.test(line[pos])) pos++;
    if (pos >= n) break;
    try {
      const obj = JSON.parse(line.slice(pos));
      out.push(obj);
      break;
    } catch (e) {
      const nextOpen = line.indexOf('{"', pos + 1);
      if (nextOpen === -1) break;
      pos = nextOpen;
    }
  }
  return out;
}

window.parseTranscript = function parseTranscript(blob) {
  const lines = blob.split(/\r?\n/);
  const events = [];
  const metaEvents = [];
  const toolCallMap = {};
  const toolResultMap = {};

  let currentTurn = null;
  let textCharsSinceTurn = 0;
  let pendingTurnBeginTs = null;
  let turnHasAssistantEvent = false;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (!line) continue;
    if (line.startsWith('{"type": "metadata"')) continue;

    let objs;
    try {
      objs = [JSON.parse(line)];
    } catch {
      objs = _iterJsonObjects(line);
      if (!objs.length) continue;
    }

    for (const obj of objs) {
      const ts = obj.timestamp;
      const msg = obj.message || {};
      const msgType = msg.type || "";
      const payload = msg.payload || {};
      const lineNum = i + 1;

      if (msgType === "TurnBegin") {
        if (currentTurn) {
          events.push({ type: "turn_end", ts: ts, line: lineNum });
        }
        let userInput = payload.user_input || "";
        if (Array.isArray(userInput)) {
          userInput = userInput.map(p => (typeof p === "object" ? p.text || "" : "")).join(" ");
        }
        events.push({ type: "user_message", ts, line: lineNum, detail: String(userInput) });
        metaEvents.push({ type: "turn_begin", ts, line: lineNum, raw: obj });
        currentTurn = { beginLine: lineNum, beginTs: ts, statusLines: [] };
        pendingTurnBeginTs = parseTimestamp(ts);
        turnHasAssistantEvent = false;
        textCharsSinceTurn = 0;
        continue;
      }

      if (msgType === "TurnEnd") {
        if (currentTurn) {
          currentTurn.endLine = lineNum;
          currentTurn.endTs = ts;
        }
        metaEvents.push({ type: "turn_end", ts, line: lineNum, raw: obj });
        currentTurn = null;
        pendingTurnBeginTs = null;
        continue;
      }

      if (msgType === "ContentPart") {
        const partType = payload.type;
        if (partType === "text") {
          textCharsSinceTurn += String(payload.text || "").length;
          events.push({ type: "assistant_text", ts, line: lineNum, detail: String(payload.text || "") });
        } else if (partType === "think") {
          events.push({ type: "thinking", ts, line: lineNum, detail: String(payload.think || "") });
        }
        if (!turnHasAssistantEvent && pendingTurnBeginTs) {
          turnHasAssistantEvent = true;
        }
        continue;
      }

      if (msgType === "ToolCall") {
        const func = payload.function || {};
        const toolName = func.name || "";
        let toolInput = {};
        try {
          toolInput = JSON.parse(func.arguments || "{}");
        } catch {
          toolInput = { _raw: func.arguments };
        }
        const tcId = payload.id || "";
        events.push({
          type: "tool_call", ts, line: lineNum,
          tool_name: toolName, tool_input: toolInput, tool_call_id: tcId,
        });
        if (tcId) toolCallMap[tcId] = events[events.length - 1];
        if (!turnHasAssistantEvent && pendingTurnBeginTs) {
          turnHasAssistantEvent = true;
        }
        continue;
      }

      if (msgType === "ToolResult") {
        const rv = payload.return_value || {};
        const tcId = payload.tool_call_id || "";
        let detail = "";
        if (typeof rv === "object") {
          const output = rv.output;
          if (Array.isArray(output)) {
            detail = output.map(x => (typeof x === "object" ? x.text || "" : String(x))).join("\n");
          } else {
            detail = String(output || "");
          }
        }
        const isError = (typeof rv === "object" && rv.is_error) || false;
        events.push({
          type: "tool_result", ts, line: lineNum,
          tool_call_id: tcId, is_error: isError, detail,
        });
        if (tcId) toolResultMap[tcId] = isError;
        continue;
      }

      if (msgType === "StatusUpdate") {
        const tu = payload.token_usage || {};
        metaEvents.push({
          type: "status_update", ts, line: lineNum,
          token_usage: tu,
          message_id: payload.message_id,
          context_tokens: payload.context_tokens,
          raw: obj,
        });
        if (currentTurn) {
          currentTurn.statusLines.push(lineNum);
        }
        continue;
      }

      // Catch-all
      metaEvents.push({ type: msgType.toLowerCase() || "unknown", ts, line: lineNum, raw: obj });
    }
  }

  // Link pairs
  for (const e of events) {
    if (e.type === "tool_result" && e.tool_call_id && toolCallMap[e.tool_call_id]) {
      e.paired_call = toolCallMap[e.tool_call_id];
      toolCallMap[e.tool_call_id].paired_result = e;
      e.is_error = toolResultMap[e.tool_call_id] || false;
    }
  }

  // Build records from status_updates
  const records = [];
  let prevInput = 0;
  const ctxTurns = [];
  let turnIdx = 0;

  for (const m of metaEvents) {
    if (m.type !== "status_update") continue;
    const tu = m.token_usage || {};
    const fresh = tu.input_other || 0;
    const create = tu.input_cache_creation || 0;
    const read = tu.input_cache_read || 0;
    const output = tu.output || 0;
    const totalInput = fresh + create + read;

    const rates = window.rateForModel("kimi-k2-6");
    const cost = (
      fresh * rates.fresh / 1e6 +
      create * rates.create / 1e6 +
      read * rates.read / 1e6 +
      output * rates.output / 1e6
    );

    records.push({
      line_num: m.line,
      uuid: m.message_id || null,
      ts: m.ts,
      model: "kimi-k2-6",
      fresh_tokens: fresh,
      cache_creation_tokens: create,
      cache_read_tokens: read,
      output_tokens: output,
      cost_usd: cost,
      text_chars: textCharsSinceTurn,
    });

    if (totalInput > 0) {
      turnIdx++;
      ctxTurns.push({
        idx: turnIdx,
        ts: m.ts ? new Date(m.ts * 1000).toISOString() : "",
        line: m.line,
        input: totalInput,
        output: output,
        delta: totalInput - prevInput,
      });
      prevInput = totalInput;
    }
  }

  return {
    records,
    ctx_turns: ctxTurns,
    turn_count: ctxTurns.length,
    events,
    meta_events: metaEvents,
  };
};

window.computeStats = function computeStats(events, metaEvents) {
  metaEvents = metaEvents || [];
  const toolCounts = {};
  let userMessages = 0;
  let assistantMessages = 0;
  let thinkingBlocks = 0;
  let toolResults = 0;
  let errorResults = 0;
  let firstTs = null;
  let lastTs = null;

  for (const e of events) {
    const dt = parseTimestamp(e.ts);
    if (dt) {
      if (!firstTs) firstTs = dt;
      lastTs = dt;
    }
    if (e.type === "tool_call") {
      toolCounts[e.tool_name] = (toolCounts[e.tool_name] || 0) + 1;
    } else if (e.type === "user_message") {
      userMessages++;
    } else if (e.type === "assistant_text") {
      assistantMessages++;
    } else if (e.type === "thinking") {
      thinkingBlocks++;
    } else if (e.type === "tool_result") {
      toolResults++;
      if (e.is_error) errorResults++;
    }
  }

  let duration = "";
  if (firstTs && lastTs) {
    const delta = (lastTs - firstTs) / 1000;
    duration = `${Math.floor(delta / 60)}m ${Math.floor(delta % 60)}s`;
  }

  const turnCount = metaEvents.filter(m => m.type === "turn_begin").length;

  const lines = [
    "SESSION STATISTICS",
    "=" * 40,
    firstTs ? `Start:      ${firstTs.toISOString().slice(0, 19)} UTC` : "",
    lastTs ? `End:        ${lastTs.toISOString().slice(0, 19)} UTC` : "",
    duration ? `Duration:   ${duration}  (wall)` : "",
    `Events:     ${events.length}`,
    `User msgs:  ${userMessages}`,
    `Asst msgs:  ${assistantMessages}`,
    `Thinking:   ${thinkingBlocks}`,
    `Tool calls: ${Object.values(toolCounts).reduce((a, b) => a + b, 0)}`,
    `Results:    ${toolResults} (${errorResults} errors)`,
    `Turns:      ${turnCount}`,
  ];

  if (Object.keys(toolCounts).length) {
    lines.push("", "TOOL USAGE:");
    for (const [tool, count] of Object.entries(toolCounts).sort((a, b) => b[1] - a[1])) {
      lines.push(`  ${tool.padEnd(20)} ${count.toString().padStart(4)}`);
    }
  }

  return lines.filter(Boolean).join("\n");
};

window.computeCache = function computeCache(metaEvents) {
  const rates = window.rateForModel("kimi-k2-6");
  let totalInputOther = 0;
  let totalOutput = 0;
  let totalCacheRead = 0;
  let totalCacheCreate = 0;
  let count = 0;

  for (const m of metaEvents) {
    if (m.type !== "status_update") continue;
    const tu = m.token_usage;
    if (!tu) continue;
    count++;
    totalInputOther += tu.input_other || 0;
    totalOutput += tu.output || 0;
    totalCacheRead += tu.input_cache_read || 0;
    totalCacheCreate += tu.input_cache_creation || 0;
  }

  if (!count) return "CACHE / TOKEN USAGE\n" + "=" * 40 + "\n(no StatusUpdate with token_usage found)";

  const totalIn = totalInputOther + totalCacheRead + totalCacheCreate;
  const hit = totalIn ? (totalCacheRead / totalIn * 100.0) : 0.0;

  const freshCost = totalInputOther * rates.fresh / 1e6;
  const cacheReadCost = totalCacheRead * rates.read / 1e6;
  const cacheCreateCost = totalCacheCreate * rates.create / 1e6;
  const outputCost = totalOutput * rates.output / 1e6;
  const totalCost = freshCost + cacheReadCost + cacheCreateCost + outputCost;

  const lines = [
    "CACHE / TOKEN USAGE",
    "=" * 40,
    `StatusUpdates: ${count}`,
    `  input total:     ${totalIn.toLocaleString().padStart(12)}`,
    `    fresh:         ${totalInputOther.toLocaleString().padStart(12)}`,
    `    cache_create:  ${totalCacheCreate.toLocaleString().padStart(12)}`,
    `    cache_read:    ${totalCacheRead.toLocaleString().padStart(12)}`,
    `  output:          ${totalOutput.toLocaleString().padStart(12)}`,
    `  hit rate:        ${hit.toFixed(1)}%`,
    "",
    "ESTIMATED BILLING",
    `  Rates (USD per 1M tokens): fresh=$${rates.fresh.toFixed(2)}  cache_read=$${rates.read.toFixed(2)}  cache_create=$${rates.create.toFixed(2)}  output=$${rates.output.toFixed(2)}`,
    `  ${"Category".padEnd(20)} ${"Cost".padStart(10)}`,
    `  ${"-".repeat(20)} ${"-".repeat(10)}`,
    freshCost ? `  ${"Fresh input".padEnd(20)} $${freshCost.toFixed(2).padStart(9)}` : "",
    cacheReadCost ? `  ${"Cache read".padEnd(20)} $${cacheReadCost.toFixed(2).padStart(9)}` : "",
    cacheCreateCost ? `  ${"Cache create".padEnd(20)} $${cacheCreateCost.toFixed(2).padStart(9)}` : "",
    outputCost ? `  ${"Output".padEnd(20)} $${outputCost.toFixed(2).padStart(9)}` : "",
    `  ${"-".repeat(20)} ${"-".repeat(10)}`,
    `  ${"TOTAL".padEnd(20)} $${totalCost.toFixed(2).padStart(9)}`,
  ];
  return lines.filter(Boolean).join("\n");
};

window.computeContextGrowth = function computeContextGrowth(metaEvents) {
  const lines = ["CONTEXT GROWTH", "=" * 40];
  const usageRecords = [];

  for (const m of metaEvents) {
    if (m.type !== "status_update") continue;
    const tu = m.token_usage;
    if (!tu) continue;
    const inputOther = tu.input_other || 0;
    const cacheRead = tu.input_cache_read || 0;
    const cacheCreate = tu.input_cache_creation || 0;
    const output = tu.output || 0;
    usageRecords.push({
      ts: m.ts,
      line: m.line,
      input: inputOther + cacheRead + cacheCreate,
      output,
    });
  }

  if (!usageRecords.length) {
    lines.push("(no StatusUpdate with token_usage found)");
    return lines.join("\n");
  }

  // Dedupe by line
  const seen = new Set();
  const deduped = [];
  for (let i = usageRecords.length - 1; i >= 0; i--) {
    const rec = usageRecords[i];
    if (!seen.has(rec.line)) {
      seen.add(rec.line);
      deduped.push(rec);
    }
  }
  deduped.reverse();

  lines.push(`  ${"#".padStart(4)}  ${"time".padEnd(19)}  ${"L#".padStart(5)}  ${"input".padStart(10)}  ${"output".padStart(8)}  ${"delta".padStart(10)}`);
  let prevInput = 0;
  for (let idx = 0; idx < deduped.length; idx++) {
    const t = deduped[idx];
    const delta = t.input - prevInput;
    lines.push(
      `  ${(idx + 1).toString().padStart(4)}  ${shortTime(t.ts).padEnd(19)}  ` +
      `L${t.line.toString().padEnd(4)}  ` +
      `${t.input.toLocaleString().padStart(10)}  ` +
      `${t.output.toLocaleString().padStart(8)}  ` +
      `${(delta >= 0 ? "+" : "") + delta.toLocaleString().padStart(9)}`
    );
    prevInput = t.input;
  }
  lines.push("", `Total: ${deduped.length} snapshots, final context: ${deduped[deduped.length - 1].input.toLocaleString()} input tokens`);
  return lines.join("\n");
};
