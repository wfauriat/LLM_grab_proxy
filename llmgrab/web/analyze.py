"""Web grab tables for `llmgrab analyze`: one row per assistant turn, traffic and stream sections."""

from collections import Counter

import pandas as pd

from llmgrab.analyze import Source, endpoint, exchanges_frame, local_ts, section
from llmgrab.web.timeline import build_model, rpc, turn_timing, usage_levels

# (category, test on host + endpoint), first match wins
CATEGORIES = [
    ("chat (Connect-RPC)", lambda host, ep: "/claudeai-rpc/" in ep),
    ("telemetry", lambda host, ep: ep.startswith(("/api/v2/rum", "/api/event_logging", "/edge-api/browser-reports"))
     or host.startswith("a.")),
    ("Cloudflare challenge", lambda host, ep: ep.startswith("/cdn-cgi/")),
    ("app API (REST)", lambda host, ep: ep.startswith(("/api/", "/v1/", "/edge-api/"))),
    ("page, i18n, assets", lambda host, ep: True),
]
EVENT_TYPES = {"6": "heartbeat", "1": "upsert", "2": "delta", "4": "usage limits", "14": "session ack"}


def turn_rows(records: list[dict]) -> list[dict]:
    model = build_model(records)
    rows = []
    for conv in model.conversations.values():
        prev = None
        for msg in model.ordered(conv):
            if msg["role"] == "assistant":
                t = turn_timing(model, msg, prev)
                levels = usage_levels(msg["limits"])
                rows.append({
                    "source": "web",
                    "time": local_ts(t["start"]),
                    "conversation": conv["uuid"][:8],
                    "kind": "chat",
                    "model": conv["model"],
                    "status": 200 if msg["done"] else 0,  # 0: no completion seen in the capture
                    "error": None if msg["done"] else "incomplete",
                    "ttfb_s": t["first_text_s"],
                    "duration_s": t["done_s"],
                    "out_chars": sum(len(b["text"]) for b in model.message_blocks(msg) if b["kind"] != "thinking"),
                    "started_s": t["started_s"],
                    "thinking_s": t["thinking_s"],
                    "deltas": t["deltas"],
                    "usage_5h": levels.get("5h"),
                    "usage_7d": levels.get("7d"),
                })
            prev = msg
    return rows


def category(host: str, ep: str) -> str:
    return next(name for name, test in CATEGORIES if test(host or "", ep))


def traffic_section(ex: pd.DataFrame) -> None:
    section("Web: traffic by category")
    ex = ex.assign(category=[category(h, e) for h, e in zip(ex.host, ex.endpoint)])
    print(ex.groupby("category").agg(
        exchanges=("category", "size"),
        endpoints=("endpoint", "nunique"),
        total_kb=("size", lambda s: round(s.sum() / 1024, 1)),
    ).sort_values("exchanges", ascending=False).to_string())


def streams_section(records: list[dict]) -> None:
    section("Web: StreamTimeline polls")
    rows = []
    for rec in records:
        body = rec["response"]["body"]
        if rpc(rec) != "StreamTimeline" or not isinstance(body, dict):
            continue
        counts, end = Counter(), ""
        for msg in body.get("connect_messages", []):
            if "end_stream" in msg:
                err = (msg["end_stream"] or {}).get("error") or {}
                end = err.get("code", "ok")
            elif isinstance(msg.get("1"), dict):
                event = msg["1"]
                counts[next((name for key, name in EVENT_TYPES.items() if key in event), "other")] += 1
        rows.append({"start": local_ts(rec["timestamp"]).strftime("%H:%M:%S"),
                     "duration_s": rec.get("duration_s"),
                     **{name: counts.get(name, 0) for name in [*EVENT_TYPES.values(), "other"]},
                     "end": end or "(open at capture end)"})
    print(pd.DataFrame(rows).to_string(index=False) if rows else "(none)")


def source(records: list[dict]) -> Source:
    calls = pd.DataFrame(turn_rows(records))
    exchanges = exchanges_frame("web", records)
    return Source("web", calls, exchanges,
                  [lambda: traffic_section(exchanges), lambda: streams_section(records)])
