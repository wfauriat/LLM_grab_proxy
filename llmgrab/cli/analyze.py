"""CLI grab tables for `llmgrab analyze`: one row per Messages API call, plus tool sections."""

import json
from collections import Counter

import pandas as pd

from llmgrab.analyze import Source, exchanges_frame, local_ts, section
from llmgrab.cli.sessions import classify, session_id
from llmgrab.records import req_body, resp_body

UPSTREAM_HOST = "api.anthropic.com"


def json_len(value) -> int:
    return len(json.dumps(value, ensure_ascii=False)) if value else 0


def to_row(rec: dict) -> dict:
    req, resp = req_body(rec), resp_body(rec)
    usage = resp.get("usage") or {}

    cache_read = usage.get("cache_read_input_tokens") or 0
    cache_write = usage.get("cache_creation_input_tokens") or 0
    uncached = usage.get("input_tokens") or 0
    total_in = uncached + cache_read + cache_write
    out = usage.get("output_tokens") or 0

    ttfb, dur = rec.get("ttfb_s"), rec.get("duration_s")
    gen_time = dur - ttfb if ttfb is not None and dur is not None else None
    content = [b for b in resp.get("content") or [] if b]

    return {
        "source": "cli",
        "time": local_ts(rec["timestamp"]),
        "conversation": (session_id(req) or "?")[:8],
        "kind": classify(req),
        "status": rec["status"],
        "model": req.get("model"),
        "ttfb_s": ttfb,
        "duration_s": dur,
        "out_chars": sum(len(b.get("text", "")) for b in content if b.get("type") == "text"),
        "n_msgs": len(req.get("messages") or []),
        "n_tools": len(req.get("tools") or []),
        "sys_chars": json_len(req.get("system")),
        "tools_chars": json_len(req.get("tools")),
        "msgs_chars": json_len(req.get("messages")),
        "in_uncached": uncached,
        "in_cache_read": cache_read,
        "in_cache_write": cache_write,
        "in_total": total_in,
        "cache_hit": round(cache_read / total_in, 3) if total_in else None,
        "out_tokens": out,
        "thinking_tokens": (usage.get("output_tokens_details") or {}).get("thinking_tokens"),
        "out_tok_per_s": round(out / gen_time, 1) if gen_time and gen_time > 0 else None,
        "stop_reason": resp.get("stop_reason"),
        "tools_called": ",".join(b["name"] for b in content if b.get("type") == "tool_use"),
        "error": (resp.get("error") or {}).get("type") if rec["status"] >= 400 else None,
    }


def tools_called_section(df: pd.DataFrame) -> None:
    section("CLI: tools called by the model")
    called = Counter(t for s in df.tools_called if s for t in s.split(","))
    for name, n in called.most_common():
        print(f"{n:5d}  {name}")
    if not called:
        print("(none)")


def tool_definitions_section(records: list[dict]) -> None:
    """Tool definitions from the most recent main call: what the tools cost in prompt size."""
    main = [r for r in records if req_body(r).get("tools")]
    if not main:
        return
    tools = req_body(main[-1])["tools"]
    section(f"CLI: tool definitions in latest main call ({len(tools)} tools)")
    sizes = sorted(((json_len(t), t.get("name", "?")) for t in tools), reverse=True)
    total = sum(s for s, _ in sizes)
    print(f"total {total:,} chars (~{total // 4:,} tokens at ~4 chars/token)")
    for size, name in sizes[:15]:
        print(f"{size:8,d}  {size / total:5.1%}  {name}")


def source(records: list[dict]) -> Source:
    calls = pd.DataFrame([to_row(r) for r in records])
    return Source("cli", calls, exchanges_frame("cli", records, UPSTREAM_HOST),
                  [lambda: tools_called_section(calls), lambda: tool_definitions_section(records)])
