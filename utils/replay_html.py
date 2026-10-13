"""Render captured Claude Code sessions as a static, chat-style HTML page.

Same event stream as replay.py (only new messages per call), laid out like a chat:
user bubbles, assistant replies rendered as Markdown, tool calls as expandable cards
holding their results, system prompts / reminders / side calls collapsed.

Usage:
    /opt/venvs/pyDS/bin/python replay_html.py [logs/*.jsonl ...] [--session 8dc6] [-o replay.html]
"""

import argparse
import html
import json
import os
from datetime import datetime
from pathlib import Path

from logtools import DEFAULT_GLOB, default_paths, load_records, req_body, resp_body
from replay import group_sessions, iter_events

try:
    from markdown_it import MarkdownIt

    # html=False: raw tags in the text (e.g. <system-reminder>) are escaped, not rendered.
    _md = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])

    def markdown(text: str) -> str:
        return _md.render(text)
except ImportError:
    def markdown(text: str) -> str:
        return f'<p class="plain">{esc(text)}</p>'


def esc(text) -> str:
    return html.escape(str(text), quote=True)


def k(n) -> str:
    """Compact token count: 65462 -> 65.5k."""
    n = n or 0
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def local_time(ts: str, fmt: str = "%H:%M:%S") -> str:
    return datetime.fromisoformat(ts).astimezone().strftime(fmt)


def one_line(text: str, limit: int = 90) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "…"


def result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    return "\n".join(c.get("text", f"[{c.get('type')}]") for c in content or [] if isinstance(c, dict))


def blocks_of(content) -> list:
    return [{"type": "text", "text": content}] if isinstance(content, str) else content or []


def first_text(content) -> str:
    return next((b.get("text", "") for b in blocks_of(content) if b.get("type") == "text"), "")


def is_reminder(text: str) -> bool:
    return text.lstrip().startswith("<system-reminder>")


class SessionRenderer:
    def __init__(self, sid: str, records: list[dict], hide_side: bool):
        self.sid, self.records, self.hide_side = sid, records, hide_side
        self.out: list[str] = []
        self.side: dict | None = None  # side call being collected into one collapsed card
        # tool_use id -> tool_result block, so results can be shown inside their tool card
        self.results: dict[str, dict] = {}
        for rec in records:
            for msg in req_body(rec).get("messages") or []:
                for b in blocks_of(msg.get("content")):
                    if b.get("type") == "tool_result":
                        self.results[b.get("tool_use_id")] = b
        self.tool_use_ids: set[str] = set()

    # ---------------------------------------------------------------- pieces

    def tool_card(self, b: dict) -> str:
        self.tool_use_ids.add(b.get("id"))
        inp = b.get("input") or {}
        arg = next((v for v in inp.values() if isinstance(v, str)), "") if isinstance(inp, dict) else ""
        res = self.results.get(b.get("id"))
        if res is None:
            status, res_html = "pending", '<div class="muted">no result captured</div>'
        else:
            status = "err" if res.get("is_error") else "ok"
            res_html = f"<pre>{esc(result_text(res))}</pre>"
        return (f'<details class="tool"><summary><span class="dot {status}"></span>'
                f'<span class="tool-name">{esc(b.get("name"))}</span>'
                f'<span class="tool-arg">{esc(one_line(arg))}</span></summary>'
                f'<div class="tool-body"><div class="label">Input</div>'
                f'<pre>{esc(json.dumps(inp, indent=2, ensure_ascii=False))}</pre>'
                f'<div class="label">Result</div>{res_html}</div></details>')

    def assistant_blocks(self, content) -> str:
        parts = []
        for b in blocks_of(content):
            kind = b.get("type")
            if kind == "text":
                parts.append(f'<div class="md">{markdown(b.get("text", ""))}</div>')
            elif kind in ("tool_use", "server_tool_use"):
                parts.append(self.tool_card(b))
            elif kind == "thinking":
                text = b.get("thinking") or ""
                parts.append(f'<details class="thinking"><summary>Thinking</summary>'
                             f'<div class="md">{markdown(text)}</div></details>' if text
                             else '<div class="chip">thinking · hidden</div>')
            elif kind == "redacted_thinking":
                parts.append('<div class="chip">thinking · redacted</div>')
            else:
                parts.append(f'<details class="sys"><summary>{esc(kind)}</summary>'
                             f'<pre>{esc(json.dumps(b, indent=2, ensure_ascii=False))}</pre></details>')
        return "".join(parts)

    def collapsed(self, title: str, text: str, cls: str = "sys") -> str:
        return (f'<details class="{cls}"><summary>{esc(title)}</summary>'
                f'<pre>{esc(text)}</pre></details>')

    def user_message(self, msg: dict, note: str = "") -> str:
        bubble, extras = [], []
        for b in blocks_of(msg.get("content")):
            kind = b.get("type")
            if kind == "text":
                text = b.get("text", "")
                if is_reminder(text):
                    extras.append(self.collapsed(f"system-reminder · {len(text):,} chars", text))
                elif text.strip():
                    bubble.append(esc(text.strip()))
            elif kind == "tool_result":
                if b.get("tool_use_id") not in self.tool_use_ids:  # orphan: its tool card wasn't shown
                    extras.append(self.collapsed("tool result (call not captured)", result_text(b)))
            else:
                extras.append(f'<div class="chip">{esc(kind)}</div>')
        html_ = "".join(extras)
        if bubble:
            label = f'<div class="note">{esc(note)}</div>' if note else ""
            html_ += f'<div class="user">{label}<div class="bubble">{"<br><br>".join(bubble)}</div></div>'
        return html_

    def message(self, msg: dict, note: str = "") -> str:
        role = msg.get("role")
        if role == "user":
            return self.user_message(msg, note)
        if role == "system":
            text = "\n\n".join(b.get("text", "") for b in blocks_of(msg.get("content")))
            return self.collapsed(f"system message (mid-conversation) · {len(text):,} chars", text)
        return f'<div class="assistant">{self.assistant_blocks(msg.get("content"))}</div>'

    def meta(self, rec: dict) -> str:
        resp = resp_body(rec)
        u = resp.get("usage") or {}
        cached = u.get("cache_read_input_tokens") or 0
        total = (u.get("input_tokens") or 0) + cached + (u.get("cache_creation_input_tokens") or 0)
        pct = f" ({cached / total:.0%} cached)" if total else ""
        bits = [resp.get("stop_reason") or "", f"in {k(total)}{pct}", f"out {k(u.get('output_tokens'))}",
                f"ttfb {rec.get('ttfb_s')}s" if rec.get("ttfb_s") is not None else "",
                f"{rec.get('duration_s')}s"]
        return (f'<div class="meta" title="{esc(json.dumps(u))}">'
                + " · ".join(esc(b) for b in bits if b) + "</div>")

    def reply(self, rec: dict) -> str:
        if rec["status"] >= 400:
            err = resp_body(rec).get("error") or {}
            return f'<div class="error">HTTP {rec["status"]} · {esc(err.get("type", ""))} {esc(err.get("message", ""))}</div>'
        return (f'<div class="assistant"><div class="avatar">✻</div>'
                f'{self.assistant_blocks(resp_body(rec).get("content"))}{self.meta(rec)}</div>')

    # ---------------------------------------------------------------- events

    def close_side(self, rec: dict) -> None:
        s, self.side = self.side, None
        if rec["status"] >= 400:
            outcome = f'HTTP {rec["status"]} {(resp_body(rec).get("error") or {}).get("type", "")}'
        else:
            outcome = "→ " + one_line(first_text(resp_body(rec).get("content")), 70)
        label = {"side": "side call", "suggestion": "suggestion", "quota_probe": "quota probe"}[s["kind"]]
        self.out.append(f'<details class="side"><summary><span class="tag">{label}</span>'
                        f'<span class="muted">{local_time(rec["timestamp"])}</span> {esc(outcome)}</summary>'
                        f'<div class="side-body">{"".join(s["parts"])}{self.reply(rec)}</div></details>')

    def render(self) -> str:
        for ev in iter_events(self.records, self.hide_side):
            t = ev["type"]
            if t == "call":
                rec = ev["rec"]
                if ev["kind"] != "main":
                    self.side = {"kind": ev["kind"], "parts": []}
                    continue
                self.out.append(f'<div class="call"><span>#{ev["index"]} · '
                                f'{local_time(rec["timestamp"])} · {esc(ev["req"].get("model", "?"))}</span></div>')
            elif t == "response":
                if self.side is not None:
                    self.close_side(ev["rec"])
                else:
                    self.out.append(self.reply(ev["rec"]))
            elif self.side is not None:  # message / side_system inside a side call
                if t == "side_system":
                    self.side["parts"].append(f'<div class="muted small">system: {esc(ev["text"])}</div>')
                elif t == "message":
                    self.side["parts"].append(self.message(ev["msg"], ev["note"]))
            elif t == "system_prompt":
                self.out.append(self.system_prompt(ev["req"]))
            elif t == "tools":
                self.out.append(self.tools(ev["names"], ev["previous"]))
            elif t == "rewritten":
                self.out.append(f'<div class="warn">History rewritten: {ev["count"]} earlier message(s) '
                                'changed or dropped (compaction / context editing?)</div>')
            elif t == "message":
                self.out.append(self.message(ev["msg"], ev["note"]))
            # "unchanged" and "previous_reply" need no output: the chat already shows them.
        return "".join(self.out)

    def system_prompt(self, req: dict) -> str:
        system = req.get("system")
        blocks = [{"text": system}] if isinstance(system, str) else system or []
        total = sum(len(b.get("text", "")) for b in blocks)
        inner = "".join(
            f'<div class="label">block {i}{" · cache " + esc(b["cache_control"].get("ttl", "5m")) if b.get("cache_control") else ""}'
            f' · {len(b.get("text", "")):,} chars</div><pre>{esc(b.get("text", ""))}</pre>'
            for i, b in enumerate(blocks))
        return (f'<details class="sys"><summary>System prompt · {len(blocks)} blocks · {total:,} chars</summary>'
                f'{inner}</details>')

    def tools(self, names: list[str], previous: list[str] | None) -> str:
        if previous is None:
            chips = "".join(f'<span class="chip">{esc(n)}</span>' for n in names)
            return f'<details class="sys"><summary>{len(names)} tools available</summary><div class="chips">{chips}</div></details>'
        added = sorted(set(names) - set(previous))
        removed = sorted(set(previous) - set(names))
        diff = " ".join([f"+{n}" for n in added] + [f"−{n}" for n in removed])
        return f'<div class="chip wide">tools changed ({len(names)}): {esc(diff)}</div>'


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
            f'{esc(", ".join(models))} · in {k(total_in)}{hit} · out {k(out)}')


CSS = """
:root{--bg:#faf9f5;--panel:#ffffff;--text:#1f1e1d;--muted:#73726c;--border:#e6e3da;--bubble:#f0eee6;
--accent:#c96442;--code:#f5f4ef;--ok:#3f8f5a;--err:#c0453a;--warn-bg:#fdf1e7;
--serif:ui-serif,Georgia,"Times New Roman",serif;--sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#262624;--panel:#30302e;--text:#ece9e1;
--muted:#a3a19a;--border:#3e3d39;--bubble:#3a3936;--code:#1f1e1d;--warn-bg:#3d2e24}}
:root[data-theme="dark"]{--bg:#262624;--panel:#30302e;--text:#ece9e1;--muted:#a3a19a;--border:#3e3d39;
--bubble:#3a3936;--code:#1f1e1d;--warn-bg:#3d2e24}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.6 var(--sans)}
main{max-width:780px;margin:0 auto;padding:32px 16px 96px}
header.page{margin-bottom:24px}
header.page h1{font:600 22px/1.3 var(--serif);margin:0 0 4px}
nav a{color:var(--accent);margin-right:12px;font-size:13px}
section.session{margin-bottom:64px}
.session-head{border-bottom:1px solid var(--border);padding-bottom:12px;margin-bottom:20px}
.session-head h2{font:600 17px/1.3 var(--serif);margin:0}
.muted{color:var(--muted)} .small{font-size:12px}
.call{display:flex;align-items:center;gap:12px;color:var(--muted);font-size:11px;margin:28px 0 10px;
text-transform:uppercase;letter-spacing:.04em}
.call::before,.call::after{content:"";flex:1;border-top:1px solid var(--border)}
.user{display:flex;flex-direction:column;align-items:flex-end;margin:12px 0}
.bubble{background:var(--bubble);border-radius:18px;padding:10px 16px;max-width:85%;white-space:pre-wrap;
overflow-wrap:anywhere}
.note{font-size:11px;color:var(--muted);margin-bottom:2px}
.assistant{position:relative;margin:12px 0;font-family:var(--serif);font-size:16px}
.avatar{color:var(--accent);font-size:18px;line-height:1;margin-bottom:6px}
.md p{margin:.5em 0} .md ul,.md ol{padding-left:1.4em}
.md code{font-family:var(--mono);font-size:.85em;background:var(--code);padding:1px 5px;border-radius:4px}
.md pre{background:var(--code);padding:12px;border-radius:8px;overflow:auto}
.md pre code{background:none;padding:0}
.md table{border-collapse:collapse;font-family:var(--sans);font-size:13px;display:block;overflow-x:auto}
.md th,.md td{border:1px solid var(--border);padding:4px 8px}
.plain{white-space:pre-wrap}
pre{font:12px/1.5 var(--mono);white-space:pre-wrap;overflow-wrap:anywhere;margin:0;max-height:420px;overflow:auto}
details{border:1px solid var(--border);border-radius:10px;background:var(--panel);margin:8px 0;
font-family:var(--sans);font-size:13px}
details>summary{cursor:pointer;padding:7px 12px;list-style:none;display:flex;align-items:center;gap:8px;
color:var(--muted)}
details>summary::-webkit-details-marker{display:none}
details>summary::before{content:"›";display:inline-block;transition:transform .15s;color:var(--muted)}
details[open]>summary::before{transform:rotate(90deg)}
details>pre,details>.chips,details>.label,details .tool-body,details .side-body{padding:0 12px 10px}
details.sys{background:transparent;border-style:dashed}
details.tool summary{color:var(--text)}
.tool-name{font-weight:600}
.tool-arg{font-family:var(--mono);font-size:12px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;
white-space:nowrap;min-width:0;flex:1}
.tool-body pre{background:var(--code);padding:8px 10px;border-radius:6px}
.label{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin:8px 0 4px}
.dot{width:8px;height:8px;border-radius:50%;flex:none;background:var(--muted)}
.dot.ok{background:var(--ok)} .dot.err{background:var(--err)}
details.thinking{background:transparent}
.chip{display:inline-block;font:12px var(--sans);color:var(--muted);border:1px solid var(--border);
border-radius:999px;padding:1px 10px;margin:2px 4px 2px 0}
.chip.wide{display:block;border-radius:8px;margin:8px 0}
.chips{display:flex;flex-wrap:wrap}
.meta{font:11px var(--sans);color:var(--muted);margin-top:6px}
details.side{background:transparent}
.tag{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--accent)}
.side-body .assistant{font-size:14px}
.error{color:var(--err);font-size:13px;margin:8px 0}
.warn{background:var(--warn-bg);border-radius:8px;padding:8px 12px;font-size:13px;margin:12px 0}
"""


def render_page(sessions: dict[str, list[dict]], hide_side: bool) -> str:
    nav = ""
    if len(sessions) > 1:
        nav = "<nav>" + "".join(f'<a href="#s-{esc(sid)}">{esc(sid[:8])}</a>' for sid in sessions) + "</nav>"
    body = "".join(
        f'<section class="session" id="s-{esc(sid)}"><div class="session-head">'
        f'<h2>Session {esc(sid[:8])}</h2><div class="muted small">{session_stats(recs)}</div></div>'
        f'{SessionRenderer(sid, recs, hide_side).render()}</section>'
        for sid, recs in sessions.items())
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Session Replay</title><style>{CSS}</style></head><body><main>'
            f'<header class="page"><h1>Claude Code session replay</h1>'
            f'<div class="muted small">generated {datetime.now():%Y-%m-%d %H:%M} from llmgrab captures</div>'
            f'{nav}</header>{body}</main></body></html>')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="*", type=Path, help=f"default: {DEFAULT_GLOB}")
    parser.add_argument("--session", help="only sessions whose id starts with this")
    parser.add_argument("--hide-side", action="store_true", help="only main-conversation calls")
    parser.add_argument("-o", "--output", type=Path,
                        default=Path(__file__).resolve().parent / "replay.html")
    args = parser.parse_args()

    sessions = group_sessions(load_records(args.files or default_paths()), args.session)
    if not sessions:
        print("no matching records")
        return

    page = render_page(sessions, args.hide_side)
    # Captures contain prompts and personal data: owner-only file.
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"wrote {args.output} ({len(page) / 1024:.0f} KB, {len(sessions)} session(s))")


if __name__ == "__main__":
    main()
