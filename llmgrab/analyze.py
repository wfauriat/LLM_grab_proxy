"""Report over captures from either grab path (or both): model calls, conversations, endpoints.

Each source provides two tables:
  calls      one row per model call (CLI) or assistant turn (web). Common columns:
             source, time, conversation, kind, model, status, error, ttfb_s (until the
             first output), duration_s, out_chars; plus source-specific columns
  exchanges  one row per HTTP exchange: source, host, method, endpoint, status,
             duration_s, size
and optional extra sections.

Usage:
    llmgrab analyze cli [data/cli/*.jsonl ...] [--csv calls.csv]
    llmgrab analyze web [data/web/claude.jsonl] [--csv calls.csv]
    llmgrab analyze all [--csv calls.csv]
"""

import argparse
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import pandas as pd

from llmgrab.paths import WEB_RECORDS
from llmgrab.records import load_records

COMMON = ["source", "time", "conversation", "kind", "model", "status", "ttfb_s", "duration_s", "out_chars"]

ID_PATTERNS = [
    re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"),  # UUID
    re.compile(r"(?<=/)[0-9a-f]{12,}(?=/|$)"),                                       # hex id segment
    re.compile(r"(?<=/)\d+(\.\d+)?(:\d+)*(:[\w-]+)?(?=/|$)"),                        # numeric segment
]
# content-hashed static files (app bundles, fonts, images): one endpoint per directory and type.
# Hex hashes ("cc43287c9-CJhUxQNN.js") or bundler suffixes ("shared-12-BMICOIGC.js").
HASHED_FILE = re.compile(r"(?<=/)[^/]*(?:[0-9a-f]{6,}[^/]*|-[\w-]{8})\.(\w+)$")


@dataclass
class Source:
    name: str
    calls: pd.DataFrame
    exchanges: pd.DataFrame
    sections: list[Callable[[], None]] = field(default_factory=list)  # source-specific report parts


def local_ts(value) -> pd.Timestamp | None:
    """ISO timestamp or epoch seconds -> naive local time (as shown in the replays)."""
    if value is None:
        return None
    ts = pd.Timestamp(value, unit="s", tz="UTC") if isinstance(value, (int, float)) else pd.Timestamp(value)
    return ts.tz_convert(datetime.now().astimezone().tzinfo).tz_localize(None).round("ms")


def endpoint(path: str | None) -> str:
    """Path without query string, ids replaced by <id>, so similar calls group together."""
    path = (path or "").split("?")[0]
    path = HASHED_FILE.sub(r"<file>.\1", path)
    for pattern in ID_PATTERNS:
        path = pattern.sub("<id>", path)
    return path


def exchanges_frame(source: str, records: list[dict], default_host: str = "") -> pd.DataFrame:
    return pd.DataFrame([{
        "source": source,
        "host": rec.get("host") or default_host,
        "method": rec.get("method"),
        "endpoint": endpoint(rec.get("path")),
        "status": rec.get("status"),
        "duration_s": rec.get("duration_s"),
        "size": rec["response"].get("size"),
    } for rec in records])


def rounded(df: pd.DataFrame) -> pd.DataFrame:
    """Floats to 3 decimals; whole-number float columns (ints mixed with NaN) as nullable ints."""
    df = df.copy()
    for c in df.select_dtypes("float").columns:
        values = df[c].dropna()
        df[c] = df[c].astype("Int64") if len(values) and (values == values.round()).all() else df[c].round(3)
    return df


def total(s: pd.Series):
    """Sum that stays empty (NaN) when a source has no such data, instead of 0."""
    return s.sum(min_count=1)


def section(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(0, 70 - len(title)))


def present(df: pd.DataFrame, cols: list[str]) -> list[str]:
    """Columns of `cols` that exist and hold at least one value."""
    return [c for c in cols if c in df and df[c].notna().any() and (df[c] != "").any()]


# ---------------------------------------------------------------- shared sections

TOKEN_COLS = ["in_uncached", "in_cache_read", "in_cache_write", "out_tokens"]
NO_REPLY_KINDS = ["count_tokens"]  # calls that generate nothing: left out of per-conversation timings
EXTRA_CALL_COLS = ["n_msgs", "in_total", "cache_hit", "out_tokens", "counted_tokens", "stop_reason", "tools_called",
                   "server_tools", "started_s", "thinking_s", "deltas", "usage_5h", "usage_7d"]


def calls_section(df: pd.DataFrame) -> None:
    section("Model calls / turns")
    print(rounded(df[present(df, COMMON + EXTRA_CALL_COLS)]).to_string(index=False))


def conversations_section(df: pd.DataFrame) -> None:
    section("Per conversation")
    ok = df[(df.status < 400) & ~df.kind.isin(NO_REPLY_KINDS)]
    agg = {"start": ("time", "min"), "end": ("time", "max"), "calls": ("kind", "size"),
           "median_ttfb_s": ("ttfb_s", "median"), "median_duration_s": ("duration_s", "median"),
           "out_chars": ("out_chars", "sum")}
    agg.update({c: (c, total) for c in present(ok, TOKEN_COLS)})
    by_conv = ok.groupby(["source", "conversation"]).agg(**agg)
    if "in_cache_read" in by_conv:
        total_in = by_conv[[c for c in TOKEN_COLS[:3] if c in by_conv]].sum(axis=1)
        by_conv["cache_hit"] = (by_conv.in_cache_read / total_in.where(total_in > 0)).round(3)
    print(rounded(by_conv).to_string())


def kinds_section(df: pd.DataFrame) -> None:
    section("By kind")
    ok = df[df.status < 400]
    agg = {"calls": ("kind", "size"), "median_ttfb_s": ("ttfb_s", "median"),
           "median_duration_s": ("duration_s", "median"), "out_chars": ("out_chars", "sum")}
    agg.update({c: (c, total) for c in present(ok, ["in_total", "out_tokens"])})
    print(rounded(ok.groupby(["source", "kind"]).agg(**agg)).to_string())


def errors_section(calls: pd.DataFrame, exchanges: pd.DataFrame) -> None:
    errors = calls[calls.status >= 400]
    failed = exchanges[exchanges.status.fillna(0) >= 400]
    if errors.empty and failed.empty:
        return
    section("Errors")
    if not errors.empty:
        print(errors[present(errors, ["source", "time", "conversation", "kind", "status", "error"])]
              .to_string(index=False))
    if not failed.empty:
        print(failed.groupby(["source", "host", "endpoint", "status"]).size().rename("count").to_string())


def endpoints_section(ex: pd.DataFrame, limit: int = 40) -> None:
    section("HTTP endpoints")
    by_ep = ex.groupby(["source", "host", "method", "endpoint"], dropna=False).agg(
        count=("endpoint", "size"),
        statuses=("status", lambda s: ",".join(str(int(x)) for x in sorted(s.dropna().unique()))),
        median_duration_s=("duration_s", "median"),
        total_kb=("size", lambda s: round(s.sum() / 1024, 1) if s.notna().any() else None),
    ).sort_values("count", ascending=False)
    with pd.option_context("display.max_colwidth", 80):
        print(rounded(by_ep.head(limit)).to_string())
    if len(by_ep) > limit:
        print(f"... {len(by_ep) - limit} more endpoints (see --csv for the call table)")


def report(sources: list[Source]) -> pd.DataFrame:
    calls = pd.concat([s.calls for s in sources], ignore_index=True).sort_values("time")
    exchanges = pd.concat([s.exchanges for s in sources], ignore_index=True)
    calls_section(calls)
    conversations_section(calls)
    kinds_section(calls)
    errors_section(calls, exchanges)
    endpoints_section(exchanges)
    for s in sources:
        for extra in s.sections:
            extra()
    return calls


# ---------------------------------------------------------------- command

def load_source(name: str, files: list[Path]) -> Source | None:
    if name == "cli":
        from llmgrab.cli.analyze import source
        from llmgrab.cli.sessions import default_paths
        records = load_records(files or default_paths())
    else:
        from llmgrab.web.analyze import source
        paths = files or ([WEB_RECORDS] if WEB_RECORDS.exists() else [])
        records = load_records(paths)
    return source(records) if records else None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="llmgrab analyze", description=__doc__.splitlines()[0])
    parser.add_argument("source", choices=["cli", "web", "all"])
    parser.add_argument("files", nargs="*", type=Path,
                        help="default: data/cli/*.jsonl (cli) or data/web/claude.jsonl (web); not with all")
    parser.add_argument("--csv", type=Path, help="also write the calls / turns table to this CSV")
    args = parser.parse_args(argv)
    if args.source == "all" and args.files:
        parser.error("files can't be given with `all`: it uses the default locations")

    names = ["cli", "web"] if args.source == "all" else [args.source]
    sources = [s for s in (load_source(n, args.files) for n in names) if s is not None]
    if not sources:
        print("no records found")
        return

    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", None)
    calls = report(sources)

    if args.csv:
        calls.to_csv(args.csv, index=False)
        print(f"\nwrote {len(calls)} rows to {args.csv}")


if __name__ == "__main__":
    main()
