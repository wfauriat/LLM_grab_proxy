"""Replay captured Claude Code sessions as a readable, turn-by-turn conversation.

Each request re-sends the full history, so for every call only the messages that
are new since the previous call of the same conversation are printed, followed by
the API's reply.

Usage:
    python replay.py [logs/*.jsonl ...] [--session 8dc6] [--full] [--hide-side] | less -R
"""

import argparse
import hashlib
import json
import sys
from datetime import datetime

from logtools import (DEFAULT_GLOB, classify, content_text, default_paths,
                      load_records, req_body, resp_body, session_id)


class Style:
    CODES = {"dim": "2", "bold": "1", "red": "31", "green": "32", "yellow": "33",
             "blue": "34", "magenta": "35", "cyan": "36"}

    def __init__(self, enabled: bool):
        self.enabled = enabled

    def __call__(self, text: str, *styles: str) -> str:
        if not self.enabled or not styles:
            return text
        codes = ";".join(self.CODES[s] for s in styles)
        return f"\033[{codes}m{text}\033[0m"


class Printer:
    def __init__(self, max_chars: int | None, style: Style):
        self.max_chars = max_chars
        self.s = style
        self.tool_names: dict[str, str] = {}  # tool_use id -> tool name

    # ---------------------------------------------------------------- helpers

    def short(self, text: str) -> str:
        text = text.strip()
        if self.max_chars and len(text) > self.max_chars:
            return text[:self.max_chars] + self.s(f" … [+{len(text) - self.max_chars} chars]", "dim")
        return text

    @staticmethod
    def emit(text: str, prefix: str = "    ") -> None:
        for line in text.splitlines() or [""]:
            print(prefix + line)

    def tool_input(self, value: dict) -> str:
        if isinstance(value, dict) and len(value) == 1:
            (key, val), = value.items()
            if isinstance(val, str):
                return f"{key}={val!r}"
        return json.dumps(value, ensure_ascii=False)

    # ---------------------------------------------------------------- blocks

    def block(self, b) -> None:
        if isinstance(b, str):
            self.emit(self.short(b))
            return
        kind = b.get("type")
        if kind == "text":
            self.emit(self.short(b.get("text", "")))
        elif kind in ("tool_use", "server_tool_use"):
            self.tool_names[b.get("id", "")] = b.get("name", "?")
            self.emit(self.s(f"[tool_use] {b.get('name')}", "yellow", "bold") + " "
                      + self.short(self.tool_input(b.get("input", {}))))
        elif kind == "tool_result":
            name = self.tool_names.get(b.get("tool_use_id"), "?")
            err = self.s(" ERROR", "red", "bold") if b.get("is_error") else ""
            self.emit(self.s(f"[tool_result ← {name}]", "yellow") + err)
            content = b.get("content")
            text = content if isinstance(content, str) else "\n".join(
                c.get("text", f"[{c.get('type')}]") for c in content or [] if isinstance(c, dict))
            self.emit(self.short(text), "      ")
        elif kind == "thinking":
            text = b.get("thinking") or ""
            label = "[thinking]" if text else "[thinking: hidden, signature only]"
            self.emit(self.s(label, "magenta") + (" " + self.short(text) if text else ""))
        elif kind == "redacted_thinking":
            self.emit(self.s("[redacted_thinking]", "magenta"))
        else:
            self.emit(self.s(f"[{kind}]", "dim") + " " + self.short(json.dumps(b, ensure_ascii=False)))

    def message(self, msg: dict, note: str = "") -> None:
        role = msg.get("role", "?")
        color = {"user": "green", "assistant": "cyan", "system": "magenta"}.get(role, "dim")
        label = {"system": "SYSTEM (mid-conversation)"}.get(role, role.upper())
        print("  " + self.s(f"▶ {label}" if role != "assistant" else f"◀ {label}", color, "bold")
              + (self.s(f"  {note}", "dim") if note else ""))
        content = msg.get("content")
        for b in [content] if isinstance(content, str) else content or []:
            self.block(b)

    # ---------------------------------------------------------------- call parts

    def header(self, i: int, rec: dict, kind: str, req: dict) -> None:
        t = datetime.fromisoformat(rec["timestamp"]).astimezone().strftime("%H:%M:%S")
        info = f"#{i}  {t}  {kind}  {req.get('model', '?')}  msgs={len(req.get('messages') or [])}"
        print("\n" + self.s(f"── {info} " + "─" * max(0, 78 - len(info)), "blue", "bold"))

    def system_prompt(self, req: dict, full: bool) -> None:
        system = req.get("system")
        blocks = [{"text": system}] if isinstance(system, str) else system or []
        sizes = " + ".join(str(len(b.get("text", ""))) for b in blocks)
        print("  " + self.s(f"⚙ SYSTEM PROMPT ({len(blocks)} blocks, {sizes} chars)", "magenta", "bold"))
        for b in blocks:
            text = b.get("text", "")
            self.emit(text if full else self.short(text.strip().splitlines()[0] if text.strip() else ""))

    def tools(self, names: list[str], previous: list[str] | None) -> None:
        if previous is None:
            print("  " + self.s(f"⚙ TOOLS ({len(names)})", "magenta", "bold"))
            self.emit(", ".join(names))
        else:
            added = sorted(set(names) - set(previous))
            removed = sorted(set(previous) - set(names))
            print("  " + self.s(f"⚙ TOOLS changed ({len(names)})", "magenta", "bold")
                  + (f"  +{added}" if added else "") + (f"  -{removed}" if removed else ""))

    def response(self, rec: dict) -> None:
        resp = resp_body(rec)
        if rec["status"] >= 400:
            err = resp.get("error") or {}
            print("  " + self.s(f"✗ HTTP {rec['status']} {err.get('type', '')}: {err.get('message', '')}", "red", "bold"))
            return
        self.message({"role": "assistant", "content": resp.get("content") or []})
        u = resp.get("usage") or {}
        timing = f"ttfb {rec.get('ttfb_s')}s, total {rec.get('duration_s')}s"
        print("    " + self.s(
            f"stop={resp.get('stop_reason')}  in={u.get('input_tokens', 0)} "
            f"cache_read={u.get('cache_read_input_tokens', 0)} cache_write={u.get('cache_creation_input_tokens', 0)} "
            f"out={u.get('output_tokens', 0)}  ({timing})", "dim"))


def normalized(value):
    """Copy without cache_control markers: Claude Code moves them between turns."""
    if isinstance(value, dict):
        return {k: normalized(v) for k, v in value.items() if k != "cache_control"}
    if isinstance(value, list):
        return [normalized(v) for v in value]
    return value


def normalized_message(msg: dict) -> dict:
    """Message with string content expanded to a text block: `"x"` == `[{"type": "text", "text": "x"}]`."""
    msg = normalized(msg)
    if isinstance(msg.get("content"), str):
        msg["content"] = [{"type": "text", "text": msg["content"]}]
    return msg


MEANINGFUL_KEYS = ("type", "text", "id", "name", "input")


def without_thinking(content) -> list:
    """Reply blocks reduced to their meaningful fields (the API adds e.g. `caller`, the client drops it)."""
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content or []
    return [{k: b[k] for k in MEANINGFUL_KEYS if k in b}
            for b in blocks if b.get("type") not in ("thinking", "redacted_thinking")]


def common_prefix(a: list, b: list) -> int:
    n = 0
    for x, y in zip(a, b):
        if normalized_message(x) != normalized_message(y):
            break
        n += 1
    return n


def digest(value) -> str:
    return hashlib.sha1(json.dumps(normalized(value), sort_keys=True).encode()).hexdigest()


def iter_events(records: list[dict], hide_side: bool = False):
    """Walk one session's calls and yield what a renderer should show, as dicts with a "type".

    Types: call, system_prompt, tools, unchanged, rewritten, previous_reply, message,
    side_system, response. Shared by the terminal and HTML renderers.
    """
    prev_msgs: list | None = None    # main-conversation history of the previous main call
    prev_reply: list | None = None   # reply content of the previous main call
    prev_system = prev_tools = None

    for i, rec in enumerate(records):
        req = req_body(rec)
        kind = classify(req)
        msgs = req.get("messages") or []
        if hide_side and kind != "main":
            continue
        yield {"type": "call", "index": i, "rec": rec, "kind": kind, "req": req}

        if kind == "quota_probe":
            yield {"type": "response", "rec": rec, "kind": kind}
            continue

        if kind == "main":
            if digest(req.get("system")) != prev_system:
                yield {"type": "system_prompt", "req": req}
                prev_system = digest(req.get("system"))
            names = [t.get("name", "?") for t in req.get("tools") or []]
            if names != prev_tools:
                yield {"type": "tools", "names": names, "previous": prev_tools}
                prev_tools = names

            start = common_prefix(prev_msgs, msgs) if prev_msgs else 0
            if prev_msgs and start < len(prev_msgs):
                yield {"type": "rewritten", "count": len(prev_msgs) - start}
            elif start:
                yield {"type": "unchanged", "count": start}
            for j, msg in enumerate(msgs[start:], start):
                if (j == start and prev_reply is not None and msg.get("role") == "assistant"
                        and without_thinking(msg.get("content")) == without_thinking(prev_reply)):
                    yield {"type": "previous_reply"}
                    continue
                yield {"type": "message", "msg": msg, "note": ""}
            yield {"type": "response", "rec": rec, "kind": kind}
            prev_msgs, prev_reply = msgs, resp_body(rec).get("content")

        elif kind == "side":  # own system prompt and own (short) conversation: show all of it
            system = req.get("system")
            if system:
                first = content_text(system if isinstance(system, str) else system[-1:]).strip()
                yield {"type": "side_system", "text": first.splitlines()[0] if first else ""}
            for msg in msgs:
                yield {"type": "message", "msg": msg, "note": ""}
            yield {"type": "response", "rec": rec, "kind": kind}

        else:  # suggestion: re-sends the main history, only its last message is new
            if msgs:
                note = f"(last of {len(msgs)} messages)" if len(msgs) > 1 else ""
                yield {"type": "message", "msg": msgs[-1], "note": note}
            yield {"type": "response", "rec": rec, "kind": kind}


def group_sessions(records: list[dict], prefix: str | None = None) -> dict[str, list[dict]]:
    """Records sorted by time and grouped by session id (optionally filtered by id prefix)."""
    sessions: dict[str, list[dict]] = {}
    for rec in sorted(records, key=lambda r: r["timestamp"]):
        sid = session_id(req_body(rec)) or "unknown"
        if not prefix or sid.startswith(prefix):
            sessions.setdefault(sid, []).append(rec)
    return sessions


def replay_session(sid: str, records: list[dict], p: Printer, args) -> None:
    print("\n" + p.s(f"═══ session {sid} — {len(records)} calls " + "═" * 50, "bold"))
    for ev in iter_events(records, args.hide_side):
        kind = ev["type"]
        if kind == "call":
            p.header(ev["index"], ev["rec"], ev["kind"], ev["req"])
        elif kind == "system_prompt":
            p.system_prompt(ev["req"], args.system)
        elif kind == "tools":
            p.tools(ev["names"], ev["previous"])
        elif kind == "rewritten":
            print("  " + p.s(f"⚠ history rewritten: {ev['count']} earlier message(s) "
                             "changed or dropped (e.g. compaction / context editing)", "red"))
        elif kind == "unchanged":
            print("  " + p.s(f"… {ev['count']} earlier message(s) unchanged", "dim"))
        elif kind == "previous_reply":
            print("  " + p.s("◀ ASSISTANT  (previous reply, shown above)", "cyan", "dim"))
        elif kind == "message":
            p.message(ev["msg"], ev["note"])
        elif kind == "side_system":
            print("  " + p.s("⚙ system: ", "magenta") + p.short(ev["text"]))
        elif kind == "response":
            p.response(ev["rec"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="*", help=f"default: {DEFAULT_GLOB}")
    parser.add_argument("--session", help="only sessions whose id starts with this")
    parser.add_argument("--max-chars", type=int, default=500, help="truncate texts (default 500)")
    parser.add_argument("--full", action="store_true", help="no truncation")
    parser.add_argument("--system", action="store_true", help="print the full system prompt")
    parser.add_argument("--hide-side", action="store_true", help="only main-conversation calls")
    parser.add_argument("--no-color", action="store_true")
    args = parser.parse_args()

    sessions = group_sessions(load_records(args.files or default_paths()), args.session)
    if not sessions:
        print("no matching records")
        return

    style = Style(enabled=sys.stdout.isatty() and not args.no_color)
    printer = Printer(None if args.full else args.max_chars, style)
    for sid, recs in sessions.items():
        replay_session(sid, recs, printer, args)


if __name__ == "__main__":
    main()
