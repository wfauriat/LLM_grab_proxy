"""Summarize Claude Code API captures written by llmgrab_addon.py.

Usage:
    python analyze.py [logs/*.jsonl ...] [--csv calls.csv]
"""

import argparse
import json
from collections import Counter
from pathlib import Path

import pandas as pd

from logtools import DEFAULT_GLOB, classify, default_paths, load_records, session_id


def json_len(value) -> int:
    return len(json.dumps(value, ensure_ascii=False)) if value else 0


def to_row(rec: dict) -> dict:
    req = rec["request"]["body"] if isinstance(rec["request"]["body"], dict) else {}
    resp = rec["response"]["body"] if isinstance(rec["response"]["body"], dict) else {}
    usage = resp.get("usage") or {}

    cache_read = usage.get("cache_read_input_tokens") or 0
    cache_write = usage.get("cache_creation_input_tokens") or 0
    uncached = usage.get("input_tokens") or 0
    total_in = uncached + cache_read + cache_write
    out = usage.get("output_tokens") or 0

    ttfb, dur = rec.get("ttfb_s"), rec.get("duration_s")
    gen_time = dur - ttfb if ttfb is not None and dur is not None else None

    return {
        "time": pd.Timestamp(rec["timestamp"]).tz_convert(None),
        "session": (session_id(req) or "?")[:8],
        "kind": classify(req),
        "status": rec["status"],
        "model": req.get("model"),
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
        "ttfb_s": ttfb,
        "duration_s": dur,
        "out_tok_per_s": round(out / gen_time, 1) if gen_time and gen_time > 0 else None,
        "stop_reason": resp.get("stop_reason"),
        "tools_called": ",".join(
            b["name"] for b in resp.get("content") or [] if b and b.get("type") == "tool_use"
        ),
        "error": (resp.get("error") or {}).get("type") if rec["status"] >= 400 else None,
    }


def section(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(0, 70 - len(title)))


def report(records: list[dict], df: pd.DataFrame) -> None:
    section("Calls")
    cols = ["time", "session", "kind", "status", "model", "n_msgs", "in_total",
            "cache_hit", "out_tokens", "ttfb_s", "duration_s", "stop_reason", "tools_called"]
    print(df[cols].to_string(index=False))

    section("Per session")
    ok = df[df.status < 400]
    by_session = ok.groupby("session").agg(
        start=("time", "min"),
        end=("time", "max"),
        calls=("kind", "size"),
        main_calls=("kind", lambda k: (k == "main").sum()),
        in_uncached=("in_uncached", "sum"),
        in_cache_read=("in_cache_read", "sum"),
        in_cache_write=("in_cache_write", "sum"),
        out_tokens=("out_tokens", "sum"),
    )
    total_in = by_session[["in_uncached", "in_cache_read", "in_cache_write"]].sum(axis=1)
    by_session["cache_hit"] = (by_session.in_cache_read / total_in).round(3)
    print(by_session.to_string())

    section("By kind")
    print(ok.groupby("kind").agg(
        calls=("kind", "size"),
        median_ttfb_s=("ttfb_s", "median"),
        median_duration_s=("duration_s", "median"),
        in_total=("in_total", "sum"),
        out_tokens=("out_tokens", "sum"),
    ).to_string())

    errors = df[df.status >= 400]
    if not errors.empty:
        section("Errors")
        print(errors[["time", "kind", "status", "error"]].to_string(index=False))

    section("Tools called by the model")
    called = Counter(t for s in df.tools_called if s for t in s.split(","))
    for name, n in called.most_common():
        print(f"{n:5d}  {name}")
    if not called:
        print("(none)")

    # Tool definitions from the most recent main call: what the tools cost in prompt size.
    main = [r for r in records if isinstance(r["request"]["body"], dict)
            and r["request"]["body"].get("tools")]
    if main:
        tools = main[-1]["request"]["body"]["tools"]
        section(f"Tool definitions in latest main call ({len(tools)} tools)")
        sizes = sorted(((json_len(t), t.get("name", "?")) for t in tools), reverse=True)
        total = sum(s for s, _ in sizes)
        print(f"total {total:,} chars (~{total // 4:,} tokens at ~4 chars/token)")
        for size, name in sizes[:15]:
            print(f"{size:8,d}  {size / total:5.1%}  {name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="*", type=Path, help=f"default: {DEFAULT_GLOB}")
    parser.add_argument("--csv", type=Path, help="also write the per-call table to this CSV")
    args = parser.parse_args()

    paths = args.files or default_paths()
    records = load_records(paths)
    if not records:
        print("no records found")
        return
    df = pd.DataFrame([to_row(r) for r in records]).sort_values("time")

    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", None)
    report(records, df)

    if args.csv:
        df.to_csv(args.csv, index=False)
        print(f"\nwrote {len(df)} rows to {args.csv}")


if __name__ == "__main__":
    main()
