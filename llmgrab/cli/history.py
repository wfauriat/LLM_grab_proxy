"""Rebuild Claude Code conversations from captured API calls (CLI grab).

Each request re-sends the full history, so for every call only the messages that
are new since the previous call of the same conversation are kept, followed by
the API's reply. Side calls (title generation, suggestions, quota probes, web
search / fetch sub-calls) become collapsed groups, and runs of count_tokens calls
one panel. The result is a source-neutral Transcript (llmgrab.transcript).
"""

import hashlib
import json
import re
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
        kind = classify(rec)
        msgs = req.get("messages") or []
        if hide_side and kind != "main":
            continue
        yield {"type": "call", "index": i, "rec": rec, "kind": kind, "req": req}

        if kind == "count_tokens":  # no reply: the call event carries all of it
            continue

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

        elif kind in ("side", "web_search", "web_fetch"):  # own system prompt and short conversation: show all
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
        sid = session_id(rec) or "unknown"
        if not prefix or sid.startswith(prefix):
            sessions.setdefault(sid, []).append(rec)
    return sessions


# --- transcript ----------------------------------------------------------

SIDE_TAGS = {"side": "side call", "suggestion": "suggestion", "quota_probe": "quota probe",
             "web_search": "web search", "web_fetch": "web fetch"}

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")  # terminal colors in captured command / tool output


def strip_ansi(text: str) -> str:
    return ANSI.sub("", text)


def blocks_of(content) -> list:
    return [{"type": "text", "text": content}] if isinstance(content, str) else content or []


def first_text(content) -> str:
    return next((b.get("text", "") for b in blocks_of(content) if b.get("type") == "text"), "")


def result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return strip_ansi(content)
    return strip_ansi("\n".join(c.get("text", f"[{c.get('type')}]") for c in content or [] if isinstance(c, dict)))


def is_reminder(text: str) -> bool:
    return text.lstrip().startswith("<system-reminder>")


# --- server tools (web search, web fetch, MCP connector): call and result in the same reply

def is_server_result(block: dict) -> bool:
    kind = block.get("type") or ""
    return kind.endswith("_tool_result") and kind != "tool_result"


def server_result(block: dict) -> tuple[str, bool]:
    """Text and error flag of a server tool result: one entry per page found or fetched."""
    content = block.get("content")
    lines, error = [], False
    for c in content if isinstance(content, list) else [content]:
        if not isinstance(c, dict):
            lines.append(str(c))
        elif c.get("error_code"):
            lines.append(f"error: {c['error_code']}")
            error = True
        elif c.get("url"):  # web_search_result, web_fetch_result (title inside its document)
            title = c.get("title") or (c.get("content") or {}).get("title") or ""
            age = f" ({c['page_age']})" if c.get("page_age") else ""
            lines.append(f"{title}{age}\n  {c['url']}")
        elif c.get("type") == "text":
            lines.append(c.get("text", ""))
        else:
            lines.append(json.dumps({k: v for k, v in c.items() if not k.startswith("encrypted")}, ensure_ascii=False))
    return "\n".join(lines), error


# --- citations: the API splits cited text into its own text blocks

def citation_source(c: dict) -> tuple[str, str]:
    """(link, title) of a citation: a web page, else a document of the request."""
    if c.get("url"):
        return c["url"], c.get("title") or c["url"]
    return "", c.get("document_title") or f"document {c.get('document_index', '?')}"


def cited_text(run: list[dict], sources: dict[str, int]) -> str:
    """Consecutive text blocks joined back, with an [n] marker after each cited part and
    the cited sources listed at the end (Markdown autolinks: clickable in HTML, plain in
    the terminal). `sources` numbers them across the whole message.
    """
    text, used, marked = "", {}, False
    for b in run:
        part = b.get("text", "")
        if marked and part[:1] in "([:":
            text += " "  # "[1](...", "[1][...]" or "[1]: ..." would parse as a link
        marks = []
        for c in b.get("citations") or []:
            link, title = citation_source(c)
            n = sources.setdefault(link or title, len(sources) + 1)
            used[n] = (link, title)
            if f"[{n}]" not in marks:
                marks.append(f"[{n}]")
        body = part.rstrip()
        text += body + (" " + " ".join(marks) if marks else "") + part[len(body):]
        marked = bool(marks) and body == part
    if used:
        # "Sources:" ends a list the text may finish with, so the source list stays a separate one
        text = text.rstrip() + "\n\nSources:\n\n" + "\n".join(f"- [{n}] {title}" + (f" — <{link}>" if link else "")
                                                             for n, (link, title) in sorted(used.items()))
    return text


# --- local commands (/context, /effort...): recorded for the model in the next user message

COMMAND_NAME = re.compile(r"<command-name>(.*?)</command-name>", re.S)
COMMAND_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S)
COMMAND_STDOUT = re.compile(r"<local-command-stdout>(.*?)</local-command-stdout>", re.S)


def starts_with_tag(text: str, tag: str) -> bool:
    return text.lstrip().startswith(f"<{tag}>")


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
        blocks = blocks_of(content)
        # server tools (web_search...) run inside the API call: their result follows in the same content
        server_results = {b.get("tool_use_id"): b for b in blocks if is_server_result(b)}
        sources: dict[str, int] = {}  # cited source -> [n], numbered across the message
        out, run = [], []             # run: consecutive text blocks, split by the API at each citation
        for b in blocks + [{}]:       # {}: end marker, flushes the last run
            kind = b.get("type")
            if kind == "text":
                run.append(b)
                continue
            if run:
                out.append(Block("text", text=cited_text(run, sources)))
                run = []
            if not b:
                break
            if kind in ("tool_use", "server_tool_use", "mcp_tool_use"):
                self.tool_names[b.get("id")] = b.get("name", "?")
                if (res := server_results.get(b.get("id"))) is not None:
                    text, error = server_result(res)
                    result, status = text, "err" if error else "ok"
                elif (res := self.results.get(b.get("id"))) is not None:
                    result, status = result_text(res), "err" if res.get("is_error") else "ok"
                else:
                    result, status = None, "pending"
                out.append(Block("tool", name=b.get("name", "?"), input=b.get("input") or {},
                                 result=result, status=status))
            elif is_server_result(b):
                if b.get("tool_use_id") not in self.tool_names:  # its call was not in this content
                    text, error = server_result(b)
                    out.append(Block("tool_result", name=str(kind), text=text, status="err" if error else "ok"))
            elif kind == "thinking":
                text = b.get("thinking") or ""
                out.append(Block("thinking", text=text, hidden=not text))
            elif kind == "redacted_thinking":
                out.append(Block("thinking", hidden=True, title="redacted"))
            else:
                out.append(Block("other", name=str(kind), data=b))
        return out

    @staticmethod
    def add_command_output(command: Block, output: str) -> None:
        """Append output to a local command block (`name` = the command line, `text` = its outputs)."""
        command.text = f"{command.text}\n\n{output}".strip()
        short = "\n" not in command.text and len(command.text) <= 60
        command.title = (f"{command.name} → {command.text}" if short
                         else f"{command.name} · {len(command.text):,} chars output")

    def user_blocks(self, content) -> list[Block]:
        out = []
        blocks = blocks_of(content)
        command = None      # last local command (/context...): its output follows in the next blocks
        after_stdout = False
        for i, b in enumerate(blocks):
            kind = b.get("type")
            if kind == "text":
                text = b.get("text", "")
                if is_reminder(text):
                    out.append(Block("collapsed", title=f"system-reminder · {len(text):,} chars", text=text))
                elif starts_with_tag(text, "local-command-caveat"):
                    out.append(Block("collapsed", title=f"local-command-caveat · {len(text):,} chars", text=text))
                elif starts_with_tag(text, "command-name"):
                    args = COMMAND_ARGS.search(text)
                    line = " ".join(x.strip() for x in (COMMAND_NAME.search(text)[1], args[1] if args else "") if x.strip())
                    command = Block("collapsed", title=f"local command {line}", name=f"local command {line}")
                    out.append(command)
                elif starts_with_tag(text, "local-command-stdout"):
                    stdout = COMMAND_STDOUT.search(text)
                    output = strip_ansi(stdout[1] if stdout else text).strip()
                    if command is None:
                        command = Block("collapsed", name="local command")
                        out.append(command)
                    self.add_command_output(command, output)
                    after_stdout = True
                    continue
                elif after_stdout and command and i < len(blocks) - 1:
                    # GUESS: text right after a command's output, not the last block (the user's own
                    # words), is a model-facing version of that output (/context sends Markdown)
                    self.add_command_output(command, text.strip())
                elif text.strip():
                    out.append(Block("text", text=text.strip()))
                after_stdout = False
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
        server = [f"{name.removesuffix('_requests')} ×{n}" for name, n in (u.get("server_tool_use") or {}).items() if n]
        iterations = len(u.get("iterations") or [])  # model turns inside the call (server tool loop)
        meta = [resp.get("stop_reason") or "", f"in {k(total)}{pct}", f"out {k(u.get('output_tokens'))}",
                *server, f"{iterations} iterations" if iterations > 1 else "",
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

    @staticmethod
    def count_tokens(items: list, rec: dict, req: dict) -> None:
        """Add a count_tokens call to the panel of the current run of such calls (e.g. /context
        sizing each part of the context: tool sets are counted with a dummy message)."""
        if not (items and isinstance(items[-1], Panel) and items[-1].title.startswith("count_tokens")):
            items.append(Panel(f"count_tokens · {local_time(rec['timestamp'])}"))
        panel = items[-1]
        tools = [t.get("name", "?") for t in req.get("tools") or []]
        system = req.get("system")
        text = "\n\n".join(x for x in (", ".join(tools), content_text(system) if system else "",
                                       *(content_text(m.get("content")) for m in req.get("messages") or [])) if x)
        counted = resp_body(rec).get("input_tokens")
        label = f"HTTP {rec['status']}" if rec["status"] >= 400 else f"{counted:,} tokens" if counted is not None else "?"
        panel.sections.append((label + (f" · {len(tools)} tools" if tools else ""), text))
        panel.title = " · ".join(panel.title.split(" · ")[:2] + [f"{len(panel.sections)} calls"])

    def build(self) -> list:
        items: list = []
        side: dict | None = None  # side call being collected into one group
        for ev in iter_events(self.records, self.hide_side):
            t = ev["type"]
            if t == "call":
                if ev["kind"] == "count_tokens":
                    self.count_tokens(items, ev["rec"], ev["req"])
                    continue
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
