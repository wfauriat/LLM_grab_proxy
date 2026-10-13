"""Rebuild claude.ai conversations from a decoded browser capture (parse_pcap.py output).

claude.ai streams each conversation over Connect-RPC (StreamTimeline) as protobuf
events. Without the schema, field numbers below were inferred from the captures:
  event:        10.1 id ("<ms>-<seq>"), then one of 1 (upsert), 2 (text delta),
                4 (usage limits), 6 (heartbeat), 14 (session ack)
  upsert 2:     conversation  1 uuid, 2 title, 6.2 model, 10.17 effort
  upsert 3:     message       1 uuid, 2 conversation, 3 role (1 human, 2 assistant),
                              4 index, 7 done, 9 created, 11 parent
  upsert 4:     block group   1 id, 2 message, 4 kind (1 content, 2 thinking)
  upsert 5:     content block 1 id, 2 group, 5 status, 9 text snapshot, 10.1 kind
                              (1 thinking, 2 text), 12 thinking title, 21 start/end
  delta:        1 block, 2 text to append | 6 thinking title
  usage limits: 13[] window: 1 name, 2.2.1 reset time, 2.3 utilization (double)
User messages are also read from PerformAction requests (2.1 uuid, 2.3 text, 2.7.2 model).

Usage:
    .venv/bin/python browsergrab/replay_web.py [captures/claude.jsonl] [-o render/web_replay.html] [--text]
"""

import argparse
import json
import os
import struct
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "utils"))
from replay_html import CSS, esc, markdown, one_line  # noqa: E402

ROLES = {1: "human", 2: "assistant"}
BLOCK_KINDS = {1: "thinking", 2: "text"}


def as_list(value):
    return value if isinstance(value, list) else [value]


def ts(value) -> float | None:
    """protobuf Timestamp {1: seconds, 2: nanos} -> epoch float."""
    if not isinstance(value, dict) or "1" not in value:
        return None
    return value["1"] + value.get("2", 0) / 1e9


def unwrap_text(value) -> str:
    """A {1: text} message decoded as text looks like "\\n<len>text": strip that prefix."""
    if isinstance(value, dict):
        return str(value.get("1", ""))
    value = str(value or "")
    if len(value) >= 2 and value[0] == "\n" and ord(value[1]) == len(value) - 2:
        return value[2:]
    return value


def as_double(value) -> float | None:
    """fixed64 kept as int by the schemaless decoder -> IEEE double."""
    return struct.unpack("<d", struct.pack("<Q", value))[0] if isinstance(value, int) else None


def load(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def rpc(rec: dict) -> str:
    return (rec.get("path") or "").rsplit("/", 1)[-1]


def timeline_events(records: list[dict]) -> list[tuple[float, dict]]:
    """All StreamTimeline events, deduplicated by id and sorted by time."""
    seen = {}
    for rec in records:
        if rpc(rec) != "StreamTimeline" or not isinstance(rec["response"]["body"], dict):
            continue
        for msg in rec["response"]["body"].get("connect_messages", []):
            event = msg.get("1")
            if isinstance(event, dict) and isinstance(event.get("10"), dict):
                event_id = event["10"]["1"]
                ms, _, seq = event_id.partition("-")
                seen[event_id] = ((int(ms), int(seq or 0)), event)
    return [(key[0] / 1000, event) for key, event in sorted(seen.values(), key=lambda kv: kv[0])]


class Model:
    """Conversations, messages, groups and blocks rebuilt from timeline events."""

    def __init__(self):
        self.conversations: dict[str, dict] = {}
        self.messages: dict[str, dict] = {}
        self.groups: dict[str, dict] = {}
        self.blocks: dict[str, dict] = {}
        self.last_assistant: dict | None = None

    def conversation(self, uuid: str) -> dict:
        return self.conversations.setdefault(uuid, {"uuid": uuid, "title": None, "model": None,
                                                    "effort": None, "messages": []})

    def message(self, uuid: str) -> dict:
        if uuid not in self.messages:
            self.messages[uuid] = {"uuid": uuid, "role": None, "index": None, "parent": None,
                                   "created": None, "done": None, "first_text": None,
                                   "groups": [], "sent": None, "limits": None, "deltas": 0}
        return self.messages[uuid]

    def apply(self, t: float, event: dict) -> None:
        if "1" in event:
            upsert = event["1"]
            if isinstance(upsert.get("2"), dict):
                self.upsert_conversation(upsert["2"])
            for m in as_list(upsert.get("3") or []):
                self.upsert_message(t, m)
            for g in as_list(upsert.get("4") or []):
                self.upsert_group(g)
            for b in as_list(upsert.get("5") or []):
                self.upsert_block(t, b)
        if isinstance(event.get("2"), dict):
            self.delta(t, event["2"])
        if isinstance(event.get("4"), dict) and self.last_assistant:
            self.last_assistant["limits"] = event["4"]

    def upsert_conversation(self, c: dict) -> None:
        conv = self.conversation(c["1"])
        conv["title"] = c.get("2") or conv["title"]
        conv["model"] = (c.get("6") or {}).get("2") or conv["model"]
        conv["effort"] = (c.get("10") or {}).get("17") or conv["effort"]

    def upsert_message(self, t: float, m: dict) -> None:
        msg = self.message(m["1"])
        msg.update(role=ROLES.get(m.get("3"), m.get("3")), index=m.get("4", msg["index"] or 0),
                   parent=m.get("11", msg["parent"]), created=ts(m.get("9")) or msg["created"])
        if m.get("7") and msg["done"] is None:
            msg["done"] = t
        if msg["role"] == "assistant":
            self.last_assistant = msg
        conv = self.conversation(m["2"])
        if msg not in conv["messages"]:
            conv["messages"].append(msg)

    def upsert_group(self, g: dict) -> None:
        group = self.groups.setdefault(g["1"], {"id": g["1"], "kind": None, "blocks": []})
        group["kind"] = g.get("4", group["kind"])
        msg = self.message(g["2"])
        if group["id"] not in msg["groups"]:
            msg["groups"].append(group["id"])

    def upsert_block(self, t: float, b: dict) -> None:
        block = self.blocks.setdefault(b["1"], {"id": b["1"], "kind": None, "text": "", "status": None,
                                               "title": None, "start": None, "end": None})
        block["kind"] = BLOCK_KINDS.get((b.get("10") or {}).get("1"), block["kind"])
        group = self.groups.setdefault(b["2"], {"id": b["2"], "kind": None, "blocks": []})
        if block["id"] not in group["blocks"]:
            group["blocks"].append(block["id"])
        if "9" in b:
            block["text"] = b["9"]  # full snapshot: replaces what deltas built so far
            msg = self.owner(block["id"])
            if b["9"] and msg and msg["role"] == "assistant":
                msg["first_text"] = msg["first_text"] or t
        block["status"] = b.get("5", block["status"])
        if "12" in b:
            block["title"] = unwrap_text(b["12"])
        times = b.get("21") or {}
        block["start"] = ts(times.get("1")) or block["start"]
        block["end"] = ts(times.get("2")) or block["end"]

    def delta(self, t: float, d: dict) -> None:
        block = self.blocks.get(d.get("1"))
        if block is None:
            return
        if "2" in d:
            block["text"] += d["2"]
            msg = self.owner(block["id"])
            if msg:
                msg["deltas"] += 1
                msg["first_text"] = msg["first_text"] or t
        if "6" in d:
            block["title"] = unwrap_text(d["6"])

    def owner(self, block_id: str) -> dict | None:
        for msg in self.messages.values():
            if any(block_id in self.groups[g]["blocks"] for g in msg["groups"] if g in self.groups):
                return msg
        return None

    def add_sent(self, records: list[dict]) -> None:
        """Attach PerformAction 'send message' requests to their human message."""
        for rec in records:
            body = rec["request"]["body"]
            if rpc(rec) != "PerformAction" or not isinstance(body, dict) or not isinstance(body.get("2"), dict):
                continue
            send = body["2"]
            msg = self.message(send["1"])
            msg["sent"] = {"at": datetime.fromisoformat(rec["timestamp"]).timestamp(), "text": send.get("3"),
                           "model": (send.get("7") or {}).get("2"), "tz": send.get("12")}
            msg["role"] = msg["role"] or "human"

    def ordered(self, conv: dict) -> list[dict]:
        return sorted(conv["messages"], key=lambda m: (m["index"] or 0, m["created"] or 0))

    def message_blocks(self, msg: dict) -> list[dict]:
        return [self.blocks[b] for g in msg["groups"] if g in self.groups
                for b in self.groups[g]["blocks"] if b in self.blocks]

    def human_text(self, msg: dict) -> str:
        text = "\n\n".join(b["text"] for b in self.message_blocks(msg) if b["text"])
        return text or (msg["sent"] or {}).get("text") or ""


# --- rendering -----------------------------------------------------------

def clock(t: float | None, fmt: str = "%H:%M:%S") -> str:
    return datetime.fromtimestamp(t).strftime(fmt) if t else "?"


def limits_text(limits: dict | None) -> str:
    if not limits:
        return ""
    parts = []
    for window in as_list(limits.get("13") or []):
        util = as_double((window.get("2") or {}).get("3"))
        parts.append(f"{window.get('1')} {util:.0%}" if util is not None else str(window.get("1")))
    return "usage " + " · ".join(parts)


def timing(model: Model, msg: dict, prev: dict | None) -> list[str]:
    """Assistant timing relative to the human message it answers."""
    start = (prev or {}).get("created") or msg["created"]
    bits = []
    if msg["created"] and start and msg["created"] != start:
        bits.append(f"started +{msg['created'] - start:.1f}s")
    for b in model.message_blocks(msg):
        if b["kind"] == "thinking" and b["start"] and b["end"]:
            bits.append(f"thinking {b['end'] - b['start']:.1f}s")
    if msg["first_text"] and start:
        bits.append(f"first text +{msg['first_text'] - start:.1f}s")
    if msg["done"] and start:
        bits.append(f"done +{msg['done'] - start:.1f}s")
    if msg["deltas"]:
        bits.append(f"{msg['deltas']} deltas")
    return bits


def render_html(model: Model, source: str) -> str:
    sections = []
    for conv in model.conversations.values():
        msgs = model.ordered(conv)
        if not msgs:
            continue
        out, prev = [], None
        for msg in msgs:
            if msg["role"] == "human":
                note = clock(msg["created"] or (msg["sent"] or {}).get("at"))
                out.append(f'<div class="user"><div class="note">{esc(note)}</div>'
                           f'<div class="bubble">{esc(model.human_text(msg).strip())}</div></div>')
            else:
                parts = []
                for b in model.message_blocks(msg):
                    if b["kind"] == "thinking":
                        label = " · ".join(x for x in (b["status"], b["title"]) if x) or "Thinking"
                        parts.append(f'<details class="thinking"><summary>{esc(label)}</summary>'
                                     '<div class="muted small" style="padding:0 12px 10px">The thinking text '
                                     'is not sent to the browser: only its status and title.</div></details>')
                    else:
                        parts.append(f'<div class="md">{markdown(b["text"])}</div>')
                meta = timing(model, msg, prev) + [limits_text(msg["limits"])]
                out.append(f'<div class="assistant"><div class="avatar">✻</div>{"".join(parts)}'
                           f'<div class="meta">{esc(" · ".join(m for m in meta if m))}</div></div>')
            prev = msg
        info = " · ".join(x for x in (clock(msgs[0]["created"], "%Y-%m-%d %H:%M"), conv["model"],
                                      f"effort {conv['effort']}" if conv["effort"] else "",
                                      f"{len(msgs)} messages", conv["uuid"]) if x)
        sections.append(f'<section class="session"><div class="session-head">'
                        f'<h2>{esc(conv["title"] or "Untitled conversation")}</h2>'
                        f'<div class="muted small">{esc(info)}</div></div>{"".join(out)}</section>')
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Web Chat Replay</title><style>{CSS}</style></head><body><main>'
            f'<header class="page"><h1>claude.ai conversation replay</h1>'
            f'<div class="muted small">generated {datetime.now():%Y-%m-%d %H:%M} from {esc(source)} '
            f'(passive browser capture)</div></header>{"".join(sections)}</main></body></html>')


def render_text(model: Model) -> str:
    lines = []
    for conv in model.conversations.values():
        msgs = model.ordered(conv)
        if not msgs:
            continue
        lines.append(f"=== {conv['title'] or 'Untitled'} · {conv['model']} · effort {conv['effort']}")
        prev = None
        for msg in msgs:
            if msg["role"] == "human":
                lines.append(f"\n--- you · {clock(msg['created'])}\n{model.human_text(msg).strip()}")
            else:
                lines.append(f"\n--- claude · {' · '.join(timing(model, msg, prev))}")
                for b in model.message_blocks(msg):
                    if b["kind"] == "thinking":
                        lines.append(f"[{b['status']}: {one_line(b['title'] or '', 100)}]")
                    else:
                        lines.append(b["text"])
                if msg["limits"]:
                    lines.append(f"[{limits_text(msg['limits'])}]")
            prev = msg
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("file", nargs="?", type=Path,
                        default=Path(__file__).resolve().parent / "captures" / "claude.jsonl")
    parser.add_argument("-o", "--output", type=Path, default=ROOT / "render" / "web_replay.html")
    parser.add_argument("--text", action="store_true", help="print the conversation instead of writing HTML")
    args = parser.parse_args()

    records = load(args.file)
    model = Model()
    for t, event in timeline_events(records):
        model.apply(t, event)
    model.add_sent(records)

    if args.text:
        print(render_text(model))
        return
    page = render_html(model, args.file.name)
    # Conversations are personal data: owner-only file.
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(page)
    n = sum(1 for c in model.conversations.values() if c["messages"])
    print(f"wrote {args.output} ({len(page) / 1024:.0f} KB, {n} conversation(s))")


if __name__ == "__main__":
    main()
