"""Transcript -> readable terminal text (ANSI colors when enabled), shared by both grab paths."""

import json

from llmgrab.transcript import Block, Conversation, Divider, Group, Message, Notice, Panel, Transcript


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


class TextRenderer:
    def __init__(self, max_chars: int | None = None, full_panels: bool = False, color: bool = False):
        self.max_chars = max_chars      # truncate long texts (None: never)
        self.full_panels = full_panels  # print panels (system prompt...) in full, not just first lines
        self.s = Style(color)
        self.lines: list[str] = []

    # ---------------------------------------------------------------- helpers

    def short(self, text: str) -> str:
        text = text.strip()
        if self.max_chars and len(text) > self.max_chars:
            return text[:self.max_chars] + self.s(f" … [+{len(text) - self.max_chars} chars]", "dim")
        return text

    def emit(self, text: str, prefix: str = "    ") -> None:
        self.lines.extend(prefix + line for line in text.splitlines() or [""])

    def head(self, text: str) -> None:
        self.lines.append("  " + text)

    @staticmethod
    def tool_input(value) -> str:
        if isinstance(value, dict) and len(value) == 1:
            (key, val), = value.items()
            if isinstance(val, str):
                return f"{key}={val!r}"
        return json.dumps(value, ensure_ascii=False)

    # ---------------------------------------------------------------- blocks

    def block(self, b: Block) -> None:
        s = self.s
        if b.kind == "text":
            self.emit(self.short(b.text))
        elif b.kind == "tool":
            self.emit(s(f"[tool_use] {b.name}", "yellow", "bold") + " " + self.short(self.tool_input(b.input)))
            if b.result is None:
                self.emit(s("(no result captured)", "dim"), "      ")
            else:
                err = s(" ERROR", "red", "bold") if b.status == "err" else ""
                self.emit(s(f"[result ← {b.name}]", "yellow") + err, "      ")
                self.emit(self.short(b.result), "        ")
        elif b.kind == "tool_result":
            err = s(" ERROR", "red", "bold") if b.status == "err" else ""
            self.emit(s(f"[tool_result{' ← ' + b.name if b.name else ''}, call not shown]", "yellow") + err)
            self.emit(self.short(b.text), "      ")
        elif b.kind == "thinking":
            if not b.hidden:
                self.emit(s(f"[{b.title or 'thinking'}]", "magenta") + " " + self.short(b.text))
            elif b.title == "redacted":
                self.emit(s("[thinking: redacted]", "magenta"))
            else:
                self.emit(s(f"[thinking: {b.title or 'hidden, signature only'}]", "magenta"))
        elif b.kind == "collapsed":
            self.emit(s(f"[{b.title}]", "dim") + " " + self.short(b.text))
        else:
            self.emit(s(f"[{b.name or b.kind}]", "dim") + " " + self.short(json.dumps(b.data, ensure_ascii=False)))

    # ---------------------------------------------------------------- items

    def message(self, m: Message) -> None:
        s = self.s
        note = s(f"  {m.note}", "dim") if m.note else ""
        if m.role == "user":
            self.head(s("▶ USER", "green", "bold") + note)
        else:
            self.head(s(f"◀ {m.role.upper()}", "cyan", "bold") + note)
        if m.error:
            self.emit(s(f"✗ {m.error}", "red", "bold"))
            return
        for b in m.blocks:
            self.block(b)
        if m.meta:
            self.emit(s(" · ".join(m.meta), "dim"))

    def panel(self, p: Panel) -> None:
        self.head(self.s(f"⚙ {p.title}", "magenta", "bold"))
        if p.chips:
            self.emit(", ".join(p.chips))
        for label, text in p.sections:
            if self.full_panels:
                if label:
                    self.emit(self.s(f"[{label}]", "dim"))
                self.emit(text)
            else:
                first = text.strip().splitlines()[0] if text.strip() else ""
                self.emit((self.s(f"[{label}] ", "dim") if label else "") + self.short(first))

    def item(self, it) -> None:
        s = self.s
        if isinstance(it, Message):
            self.message(it)
        elif isinstance(it, Divider):
            info = f"{it.label}  {it.detail}".strip()
            self.lines.append("\n" + s(f"── {info} " + "─" * max(0, 78 - len(info)), "blue", "bold"))
        elif isinstance(it, Panel):
            self.panel(it)
        elif isinstance(it, Notice):
            style = {"muted": ("dim",), "info": ("magenta",), "warn": ("red",)}[it.level]
            self.head(s(("⚠ " if it.level == "warn" else "") + it.text, *style))
        elif isinstance(it, Group):
            info = " · ".join(x for x in (it.tag, it.label, it.detail) if x)
            self.lines.append("\n" + s(f"┄┄ {info} ┄┄ ", "blue") + s(it.summary, "dim"))
            for x in it.items:
                self.item(x)
        else:
            raise TypeError(f"unknown transcript item: {it!r}")

    def conversation(self, c: Conversation) -> None:
        self.lines.append("\n" + self.s(f"═══ {c.title} — {c.subtitle} " + "═" * 20, "bold"))
        for it in c.items:
            self.item(it)


def render(t: Transcript, max_chars: int | None = None, full_panels: bool = False, color: bool = False) -> str:
    r = TextRenderer(max_chars, full_panels, color)
    for c in t.conversations:
        r.conversation(c)
    return "\n".join(r.lines) + "\n"
