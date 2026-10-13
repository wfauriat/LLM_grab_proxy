"""Rebuild Claude Code conversations from captured API calls (CLI grab).

Each request re-sends the full history, so for every call only the messages that
are new since the previous call of the same conversation are kept, followed by
the API's reply. Side calls (title generation, suggestions, quota probes) become
collapsed groups. The result is a source-neutral Transcript (llmgrab.transcript).
"""

import hashlib
import json
from datetime import datetime

from llmgrab.cli.sessions import classify, content_text, session_id
from llmgrab.records import req_body, resp_body
from llmgrab.transcript import (Block, Conversation, Divider, Group, Message, Notice, Panel, Transcript,
                                k, local_time, one_line)


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


# --- transcript ----------------------------------------------------------

SIDE_TAGS = {"side": "side call", "suggestion": "suggestion", "quota_probe": "quota probe"}


def blocks_of(content) -> list:
    return [{"type": "text", "text": content}] if isinstance(content, str) else content or []


def first_text(content) -> str:
    return next((b.get("text", "") for b in blocks_of(content) if b.get("type") == "text"), "")


def result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    return "\n".join(c.get("text", f"[{c.get('type')}]") for c in content or [] if isinstance(c, dict))


def is_reminder(text: str) -> bool:
    return text.lstrip().startswith("<system-reminder>")


class SessionBuilder:
    """One Claude Code session -> Conversation (tool results are shown inside their tool call)."""

    def __init__(self, sid: str, records: list[dict], hide_side: bool):
        self.sid, self.records, self.hide_side = sid, records, hide_side
        # tool_use id -> tool_result block, taken from the request that follows the call
        self.results: dict[str, dict] = {}
        for rec in records:
            for msg in req_body(rec).get("messages") or []:
                for b in blocks_of(msg.get("content")):
                    if b.get("type") == "tool_result":
                        self.results[b.get("tool_use_id")] = b
        self.tool_names: dict[str, str] = {}  # tool_use ids already shown -> tool name

    # ---------------------------------------------------------------- messages

    def assistant_blocks(self, content) -> list[Block]:
        out = []
        for b in blocks_of(content):
            kind = b.get("type")
            if kind == "text":
                out.append(Block("text", text=b.get("text", "")))
            elif kind in ("tool_use", "server_tool_use"):
                self.tool_names[b.get("id")] = b.get("name", "?")
                res = self.results.get(b.get("id"))
                out.append(Block("tool", name=b.get("name", "?"), input=b.get("input") or {},
                                 result=None if res is None else result_text(res),
                                 status="pending" if res is None else "err" if res.get("is_error") else "ok"))
            elif kind == "thinking":
                text = b.get("thinking") or ""
                out.append(Block("thinking", text=text, hidden=not text))
            elif kind == "redacted_thinking":
                out.append(Block("thinking", hidden=True, title="redacted"))
            else:
                out.append(Block("other", name=str(kind), data=b))
        return out

    def user_blocks(self, content) -> list[Block]:
        out = []
        for b in blocks_of(content):
            kind = b.get("type")
            if kind == "text":
                text = b.get("text", "")
                if is_reminder(text):
                    out.append(Block("collapsed", title=f"system-reminder · {len(text):,} chars", text=text))
                elif text.strip():
                    out.append(Block("text", text=text.strip()))
            elif kind == "tool_result":
                if b.get("tool_use_id") not in self.tool_names:  # its tool call was not shown
                    out.append(Block("tool_result", text=result_text(b),
                                     status="err" if b.get("is_error") else "ok"))
            else:
                out.append(Block("other", name=str(kind), data=b))
        return out

    def message(self, msg: dict, note: str = ""):
        role = msg.get("role")
        if role == "system":
            text = "\n\n".join(b.get("text", "") for b in blocks_of(msg.get("content")))
            return Panel(f"system message (mid-conversation) · {len(text):,} chars", [("", text)])
        if role == "user":
            blocks = self.user_blocks(msg.get("content"))
            # nothing left when it only carried results already shown in their tool calls
            return Message("user", blocks, note=note) if blocks else None
        return Message("assistant", self.assistant_blocks(msg.get("content")), note=note)

    def reply(self, rec: dict) -> Message:
        resp = resp_body(rec)
        if rec["status"] >= 400:
            err = resp.get("error") or {}
            return Message("assistant", reply=True,
                           error=f"HTTP {rec['status']} · {err.get('type', '')} {err.get('message', '')}")
        u = resp.get("usage") or {}
        cached = u.get("cache_read_input_tokens") or 0
        total = (u.get("input_tokens") or 0) + cached + (u.get("cache_creation_input_tokens") or 0)
        pct = f" ({cached / total:.0%} cached)" if total else ""
        meta = [resp.get("stop_reason") or "", f"in {k(total)}{pct}", f"out {k(u.get('output_tokens'))}",
                f"ttfb {rec.get('ttfb_s')}s" if rec.get("ttfb_s") is not None else "",
                f"{rec.get('duration_s')}s"]
        return Message("assistant", self.assistant_blocks(resp.get("content")), reply=True,
                       meta=[m for m in meta if m], meta_detail=json.dumps(u))

    # ---------------------------------------------------------------- call parts

    @staticmethod
    def system_prompt(req: dict) -> Panel:
        system = req.get("system")
        blocks = [{"text": system}] if isinstance(system, str) else system or []
        total = sum(len(b.get("text", "")) for b in blocks)
        sections = [(f'block {i}{" · cache " + b["cache_control"].get("ttl", "5m") if b.get("cache_control") else ""}'
                     f' · {len(b.get("text", "")):,} chars', b.get("text", ""))
                    for i, b in enumerate(blocks)]
        return Panel(f"System prompt · {len(blocks)} blocks · {total:,} chars", sections)

    @staticmethod
    def tools(names: list[str], previous: list[str] | None):
        if previous is None:
            return Panel(f"{len(names)} tools available", chips=names)
        added = sorted(set(names) - set(previous))
        removed = sorted(set(previous) - set(names))
        diff = " ".join([f"+{n}" for n in added] + [f"−{n}" for n in removed])
        return Notice(f"tools changed ({len(names)}): {diff}")

    def side_group(self, kind: str, rec: dict, req: dict, items: list) -> Group:
        if rec["status"] >= 400:
            outcome = f'HTTP {rec["status"]} {(resp_body(rec).get("error") or {}).get("type", "")}'
        else:
            outcome = "→ " + one_line(first_text(resp_body(rec).get("content")), 70)
        return Group(SIDE_TAGS[kind], local_time(rec["timestamp"]), outcome, items + [self.reply(rec)],
                     detail=f"{req.get('model', '?')} msgs={len(req.get('messages') or [])}")

    def build(self) -> list:
        items: list = []
        side: dict | None = None  # side call being collected into one group
        for ev in iter_events(self.records, self.hide_side):
            t = ev["type"]
            if t == "call":
                if ev["kind"] != "main":
                    side = {"kind": ev["kind"], "rec": ev["rec"], "req": ev["req"], "items": []}
                    continue
                req = ev["req"]
                items.append(Divider(f'#{ev["index"]} · {local_time(ev["rec"]["timestamp"])} · {req.get("model", "?")}',
                                     detail=f'main msgs={len(req.get("messages") or [])}'))
            elif t == "response":
                if side is not None:
                    items.append(self.side_group(side["kind"], side["rec"], side["req"], side["items"]))
                    side = None
                else:
                    items.append(self.reply(ev["rec"]))
            elif side is not None:  # message / side_system inside a side call
                if t == "side_system":
                    side["items"].append(Notice(f'system: {ev["text"]}', level="muted"))
                elif t == "message" and (m := self.message(ev["msg"], ev["note"])):
                    side["items"].append(m)
            elif t == "system_prompt":
                items.append(self.system_prompt(ev["req"]))
            elif t == "tools":
                items.append(self.tools(ev["names"], ev["previous"]))
            elif t == "rewritten":
                items.append(Notice(f'History rewritten: {ev["count"]} earlier message(s) changed or dropped '
                                    '(compaction / context editing?)', level="warn"))
            elif t == "unchanged":
                items.append(Notice(f'… {ev["count"]} earlier message(s) unchanged', level="muted", quiet=True))
            elif t == "previous_reply":
                items.append(Notice("◀ previous reply, shown above", level="muted", quiet=True))
            elif t == "message" and (m := self.message(ev["msg"], ev["note"])):
                items.append(m)
        return items


def session_stats(records: list[dict]) -> str:
    usage = [resp_body(r).get("usage") or {} for r in records]
    cache_read = sum(u.get("cache_read_input_tokens") or 0 for u in usage)
    total_in = cache_read + sum((u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
                                for u in usage)
    out = sum(u.get("output_tokens") or 0 for u in usage)
    models = sorted({req_body(r).get("model") for r in records if req_body(r).get("model")})
    start, end = records[0]["timestamp"], records[-1]["timestamp"]
    hit = f" · {cache_read / total_in:.0%} cached" if total_in else ""
    return (f'{local_time(start, "%Y-%m-%d %H:%M")}–{local_time(end, "%H:%M")} · {len(records)} calls · '
            f'{", ".join(models)} · in {k(total_in)}{hit} · out {k(out)}')


def build_transcript(records: list[dict], session_prefix: str | None = None,
                     hide_side: bool = False) -> Transcript:
    sessions = group_sessions(records, session_prefix)
    return Transcript(
        title="Session Replay",
        heading="Claude Code session replay",
        subtitle=f"generated {datetime.now():%Y-%m-%d %H:%M} from llmgrab captures",
        conversations=[Conversation(sid, f"Session {sid[:8]}", session_stats(recs),
                                    SessionBuilder(sid, recs, hide_side).build())
                       for sid, recs in sessions.items()])
