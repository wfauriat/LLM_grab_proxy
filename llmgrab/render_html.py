"""Transcript -> standalone chat-style HTML page (light/dark), shared by both grab paths."""

import html
import json

from markdown_it import MarkdownIt

from llmgrab.transcript import Block, Conversation, Divider, Group, Message, Notice, Panel, Transcript, one_line

# html=False: raw tags in the text (e.g. <system-reminder>) are escaped, not rendered.
_md = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])


def markdown(text: str) -> str:
    return _md.render(text)


def esc(text) -> str:
    return html.escape(str(text), quote=True)


def collapsed(title: str, text: str, cls: str = "sys") -> str:
    return f'<details class="{cls}"><summary>{esc(title)}</summary><pre>{esc(text)}</pre></details>'


# ---------------------------------------------------------------- blocks

def tool_card(b: Block) -> str:
    inp = b.input if b.input is not None else {}
    arg = next((v for v in inp.values() if isinstance(v, str)), "") if isinstance(inp, dict) else ""
    res_html = ('<div class="muted">no result captured</div>' if b.result is None
                else f"<pre>{esc(b.result)}</pre>")
    return (f'<details class="tool"><summary><span class="dot {b.status}"></span>'
            f'<span class="tool-name">{esc(b.name)}</span>'
            f'<span class="tool-arg">{esc(one_line(arg))}</span></summary>'
            f'<div class="tool-body"><div class="label">Input</div>'
            f'<pre>{esc(json.dumps(inp, indent=2, ensure_ascii=False))}</pre>'
            f'<div class="label">Result</div>{res_html}</div></details>')


def thinking(b: Block) -> str:
    if not b.hidden:
        return (f'<details class="thinking"><summary>{esc(b.title or "Thinking")}</summary>'
                f'<div class="md">{markdown(b.text)}</div></details>')
    if b.title and b.title != "redacted":
        return (f'<details class="thinking"><summary>{esc(b.title)}</summary>'
                '<div class="muted small" style="padding:0 12px 10px">The thinking text was not sent to the '
                'client: only its status and title.</div></details>')
    return f'<div class="chip">thinking · {"redacted" if b.title == "redacted" else "hidden"}</div>'


def assistant_block(b: Block) -> str:
    if b.kind == "text":
        return f'<div class="md">{markdown(b.text)}</div>'
    if b.kind == "tool":
        return tool_card(b)
    if b.kind == "thinking":
        return thinking(b)
    if b.kind == "collapsed":
        return collapsed(b.title, b.text)
    return (f'<details class="sys"><summary>{esc(b.name)}</summary>'
            f'<pre>{esc(json.dumps(b.data, indent=2, ensure_ascii=False))}</pre></details>')


def user_message(m: Message) -> str:
    bubble, extras = [], []
    for b in m.blocks:
        if b.kind == "text":
            bubble.append(esc(b.text))
        elif b.kind == "collapsed":
            extras.append(collapsed(b.title, b.text))
        elif b.kind == "tool_result":
            extras.append(collapsed(f"tool result{' ← ' + b.name if b.name else ''} (call not captured)", b.text))
        else:
            extras.append(f'<div class="chip">{esc(b.name or b.kind)}</div>')
    out = "".join(extras)
    if bubble:
        label = f'<div class="note">{esc(m.note)}</div>' if m.note else ""
        out += f'<div class="user">{label}<div class="bubble">{"<br><br>".join(bubble)}</div></div>'
    return out


def assistant_message(m: Message) -> str:
    if m.error:
        return f'<div class="error">{esc(m.error)}</div>'
    avatar = '<div class="avatar">✻</div>' if m.reply else ""
    meta = ""
    if m.meta:
        title = f' title="{esc(m.meta_detail)}"' if m.meta_detail else ""
        meta = f'<div class="meta"{title}>' + " · ".join(esc(x) for x in m.meta) + "</div>"
    return f'<div class="assistant">{avatar}{"".join(assistant_block(b) for b in m.blocks)}{meta}</div>'


# ---------------------------------------------------------------- items

def panel(p: Panel) -> str:
    if p.chips:
        chips = "".join(f'<span class="chip">{esc(c)}</span>' for c in p.chips)
        return f'<details class="sys"><summary>{esc(p.title)}</summary><div class="chips">{chips}</div></details>'
    inner = "".join((f'<div class="label">{esc(label)}</div>' if label else "") + f"<pre>{esc(text)}</pre>"
                    for label, text in p.sections)
    return f'<details class="sys"><summary>{esc(p.title)}</summary>{inner}</details>'


NOTICE_CLASS = {"info": "chip wide", "warn": "warn", "muted": "muted small"}


def item(it) -> str:
    if isinstance(it, Message):
        return user_message(it) if it.role == "user" else assistant_message(it)
    if isinstance(it, Divider):
        return f'<div class="call"><span>{esc(it.label)}</span></div>'
    if isinstance(it, Panel):
        return panel(it)
    if isinstance(it, Notice):
        return "" if it.quiet else f'<div class="{NOTICE_CLASS[it.level]}">{esc(it.text)}</div>'
    if isinstance(it, Group):
        return (f'<details class="side"><summary><span class="tag">{esc(it.tag)}</span>'
                f'<span class="muted">{esc(it.label)}</span> {esc(it.summary)}</summary>'
                f'<div class="side-body">{"".join(item(x) for x in it.items)}</div></details>')
    raise TypeError(f"unknown transcript item: {it!r}")


def conversation(c: Conversation) -> str:
    return (f'<section class="session" id="s-{esc(c.id)}"><div class="session-head">'
            f'<h2>{esc(c.title)}</h2><div class="muted small">{esc(c.subtitle)}</div></div>'
            f'{"".join(item(x) for x in c.items)}</section>')


def render(t: Transcript) -> str:
    """Complete standalone page."""
    nav = ""
    if len(t.conversations) > 1:
        nav = "<nav>" + "".join(f'<a href="#s-{esc(c.id)}">{esc(c.id[:8])}</a>' for c in t.conversations) + "</nav>"
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{esc(t.title)}</title><style>{CSS}</style></head><body><main>'
            f'<header class="page"><h1>{esc(t.heading)}</h1>'
            f'<div class="muted small">{esc(t.subtitle)}</div>{nav}</header>'
            f'{"".join(conversation(c) for c in t.conversations)}</main></body></html>')


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
