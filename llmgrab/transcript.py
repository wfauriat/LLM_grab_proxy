"""Source-neutral conversation model, built from CLI or web captures and rendered as HTML or text.

A Transcript holds Conversations; a conversation is a list of items shown in order:
Divider (call boundary), Message (user / assistant / system, made of Blocks),
Panel (collapsed reference text: system prompt, tool list), Notice (one-line remark)
and Group (a collapsed sub-sequence, e.g. a Claude Code side call).
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


def k(n) -> str:
    """Compact token count: 65462 -> 65.5k."""
    n = n or 0
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def local_time(ts: str | float | None, fmt: str = "%H:%M:%S") -> str:
    """ISO timestamp or epoch seconds -> local time text."""
    if ts is None:
        return "?"
    dt = datetime.fromtimestamp(ts) if isinstance(ts, (int, float)) else datetime.fromisoformat(ts).astimezone()
    return dt.strftime(fmt)


def one_line(text: str, limit: int = 90) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "…"


@dataclass
class Block:
    """One piece of a message.

    kind:
      text        Markdown text (assistant) or plain text (user)
      thinking    `text` when available, else `hidden`; `title` = status / summary line
      tool        `name`, `input`, and `result` / `status` ("ok", "err", "pending")
      tool_result a result whose tool call is not shown (`name` when known)
      collapsed   reference text shown folded (`title`), e.g. a system reminder
      other       unknown block type (`name`), raw JSON in `data`
    """
    kind: str
    text: str = ""
    title: str = ""
    hidden: bool = False
    name: str = ""
    input: Any = None
    result: str | None = None
    status: str = "pending"
    data: Any = None


@dataclass
class Message:
    role: str                    # "user", "assistant" or "system"
    blocks: list[Block] = field(default_factory=list)
    reply: bool = False          # the reply received for a call (vs. history re-sent in a request)
    note: str = ""               # small label (time, "(last of 12 messages)")
    meta: list[str] = field(default_factory=list)  # footer: stop reason, tokens, timings, usage
    meta_detail: str = ""        # raw detail for the footer (HTML tooltip)
    error: str = ""              # failed call: shown instead of blocks


@dataclass
class Divider:
    label: str                   # "#3 · 10:15:02 · claude-opus-5-5"
    detail: str = ""             # terminal only: "main msgs=12"


@dataclass
class Panel:
    title: str
    sections: list[tuple[str, str]] = field(default_factory=list)  # (label, text)
    chips: list[str] = field(default_factory=list)


@dataclass
class Notice:
    text: str
    level: str = "info"          # "muted" (aside), "info" (change worth seeing), "warn"
    quiet: bool = False          # context the HTML layout already makes obvious: terminal only


@dataclass
class Group:
    tag: str                     # "side call"
    label: str                   # time
    summary: str                 # "→ first words of the reply"
    items: list = field(default_factory=list)
    detail: str = ""             # terminal only: model, message count


@dataclass
class Conversation:
    id: str
    title: str
    subtitle: str = ""
    items: list = field(default_factory=list)


@dataclass
class Transcript:
    heading: str                 # page heading
    subtitle: str                # "generated ... from ..."
    conversations: list[Conversation] = field(default_factory=list)
    title: str = "Replay"        # HTML <title>
