"""Best-guess flattening of a captured Messages API request into the one text the model reads.

The API takes structured JSON (tools, system, messages made of content blocks); the model
takes one token sequence. The server renders the former into the latter with a private
template, then tokenizes it. This module emulates that rendering on captured requests. It
shows the shape of the model input, not Anthropic's exact text.

Templates:
- anthropic: a guess. Turn markers of the legacy Text Completions API ("\\n\\nHuman:" /
  "\\n\\nAssistant:"); tools, tool calls and tool results in the XML style of Anthropic's
  published legacy tool-use format; tools before the system prompt (the documented render
  order tools -> system -> messages).
- chatml: what an open-weight server (vLLM, llama.cpp) would build from the same request
  with a Qwen-style chat template, for comparison.

Further guesses, marked GUESS below: the billing line Claude Code puts first in the system
prompt is dropped; effort appears as a tag; thinking from earlier turns, whose text the
client never holds, is a placeholder (presumably restored server-side from its signature).
Images and documents become image embeddings, not text, so they are placeholders too.
"""

import argparse
import itertools
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from llmgrab.cli.sessions import classify, default_paths, session_id
from llmgrab.paths import open_private
from llmgrab.records import load_records, req_body, resp_body

BILLING_PREFIX = "x-anthropic-billing-header:"
CACHE_MARK = "⟦cache breakpoint⟧"


@dataclass
class Part:
    kind: str  # text | thinking | tool_use | tool_result | media | other
    text: str = ""
    name: str = ""
    data: object = None  # tool input, tool result content, signature length, raw block
    error: bool = False
    cache: bool = False


@dataclass
class Turn:
    role: str  # user | assistant | system (mid-conversation)
    parts: list[Part]


@dataclass
class Prompt:
    tools: list[dict]
    system: list[Part]
    turns: list[Turn] = field(default_factory=list)
    effort: str | None = None


# --- request JSON -> Prompt ---------------------------------------------------------------

def as_blocks(content) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content or [] if isinstance(b, dict)]


def to_part(block: dict, tool_names: dict) -> Part:
    kind, cache = block.get("type"), "cache_control" in block
    if kind == "text":
        return Part("text", text=block.get("text", ""), cache=cache)
    if kind in ("thinking", "redacted_thinking"):
        sealed = block.get("signature") or block.get("data") or ""
        return Part("thinking", text=block.get("thinking") or "", data=len(sealed), cache=cache)
    if kind in ("tool_use", "server_tool_use"):
        tool_names[block.get("id")] = block.get("name", "?")
        return Part("tool_use", name=block.get("name", "?"), data=block.get("input") or {}, cache=cache)
    if kind == "tool_result":
        return Part("tool_result", name=tool_names.get(block.get("tool_use_id"), "?"),
                    data=block.get("content"), error=bool(block.get("is_error")), cache=cache)
    if kind in ("image", "document"):
        src = block.get("source") or {}
        what = src.get("media_type") or src.get("url") or src.get("file_id") or src.get("type", "")
        return Part("media", name=kind, text=what, data=len(src.get("data") or ""), cache=cache)
    return Part("other", name=kind or "?", data=block, cache=cache)


def clear_thinking(turns: list[Turn], context_management: dict | None) -> None:
    """Apply a clear_thinking edit: keep thinking only in the last N assistant turns that have it."""
    for edit in (context_management or {}).get("edits") or []:
        if edit.get("type") != "clear_thinking_20251015":
            continue
        keep = edit.get("keep", {"type": "thinking_turns", "value": 1})  # API default: 1 turn
        if keep == "all":
            continue
        n = keep.get("value", 1) if isinstance(keep, dict) else 1
        thinking_turns = [t for t in turns if t.role == "assistant" and any(p.kind == "thinking" for p in t.parts)]
        for turn in thinking_turns[:max(0, len(thinking_turns) - n)]:
            turn.parts = [p for p in turn.parts if p.kind != "thinking"]


def parse_request(req: dict) -> Prompt:
    tool_names = {}  # tool_use_id -> tool name, so results can name their tool
    system = [to_part(b, tool_names) for b in as_blocks(req.get("system"))
              if not b.get("text", "").startswith(BILLING_PREFIX)]  # GUESS: billing metadata, not prompt
    turns = [Turn(m.get("role", "?"), [to_part(b, tool_names) for b in as_blocks(m.get("content"))])
             for m in req.get("messages") or []]
    clear_thinking(turns, req.get("context_management"))
    if req.get("cache_control") and turns and turns[-1].parts:  # top-level auto-caching: last block
        turns[-1].parts[-1].cache = True
    return Prompt(req.get("tools") or [], system, turns, (req.get("output_config") or {}).get("effort"))


# --- Prompt -> text -----------------------------------------------------------------------

def value_text(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def result_text(content) -> str:
    """tool_result content: a string, or text/image blocks."""
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") if b.get("type") == "text" else f"[{b.get('type')} block]"
                     for b in as_blocks(content))


class Template:
    def __init__(self, marks: bool):
        self.marks = marks

    def render(self, prompt: Prompt) -> str:
        return self.preamble(prompt) + "".join(self.turn(t) for t in prompt.turns) + self.generation_prompt()

    def body(self, parts: list[Part]) -> str:
        """Render a turn's parts; consecutive tool calls (or results) are rendered as one run."""
        chunks = []
        for kind, run in itertools.groupby(parts, key=lambda p: p.kind):
            run = list(run)
            if kind in ("tool_use", "tool_result"):
                chunks.append(self.mark(getattr(self, kind)(run), run))
            else:
                chunks += [self.mark(getattr(self, kind)(p), [p]) for p in run]
        return "\n\n".join(c for c in chunks if c)

    def mark(self, text: str, run: list[Part]) -> str:
        return text + (CACHE_MARK if self.marks and any(p.cache for p in run) else "")

    def text(self, p: Part) -> str:
        return p.text

    def media(self, p: Part) -> str:
        return f"[{p.name} {p.text} ({p.data} base64 chars): image embeddings here, not text tokens]"

    def other(self, p: Part) -> str:
        return f"[{p.name} block: {json.dumps(p.data, ensure_ascii=False)[:500]}]"


class AnthropicGuess(Template):
    TURN = {"user": "\n\nHuman:", "assistant": "\n\nAssistant:", "system": "\n\nSystem:"}  # System: GUESS

    TOOLS_INTRO = (
        "In this environment you have access to a set of tools you can use to answer the user's question.\n"
        "You can invoke functions by writing a \"<function_calls>\" block like the following as part of "
        "your reply to the user:\n"
        "<function_calls>\n<invoke name=\"$FUNCTION_NAME\">\n"
        "<parameter name=\"$PARAMETER_NAME\">$PARAMETER_VALUE</parameter>\n...\n</invoke>\n</function_calls>\n\n"
        "String and scalar parameters should be specified as is, while lists and objects should use JSON format.\n\n"
        "Here are the functions available in JSONSchema format:\n")

    def preamble(self, prompt: Prompt) -> str:
        sections = []
        if prompt.tools:
            functions = "\n".join(
                "<function>" + json.dumps({"description": t.get("description", ""), "name": t.get("name"),
                                           "parameters": t.get("input_schema")} if "input_schema" in t else t,
                                          ensure_ascii=False) + "</function>"
                for t in prompt.tools)
            sections.append(f"{self.TOOLS_INTRO}<functions>\n{functions}\n</functions>")
        if prompt.effort:
            sections.append(f"<reasoning_effort>{prompt.effort}</reasoning_effort>")  # GUESS
        sections.append(self.body(prompt.system))
        return "\n\n".join(s for s in sections if s)

    def turn(self, turn: Turn) -> str:
        return f"{self.TURN.get(turn.role, f'\n\n{turn.role}:')} {self.body(turn.parts)}"

    def generation_prompt(self) -> str:
        return self.TURN["assistant"]

    def thinking(self, p: Part) -> str:
        if p.text:
            return f"<thinking>{p.text}</thinking>"
        return f"<thinking>[reasoning not held by the client; {p.data}-char signature, presumably restored server-side]</thinking>"

    def tool_use(self, run: list[Part]) -> str:
        invokes = "\n".join(
            f"<invoke name=\"{p.name}\">\n"
            + "".join(f"<parameter name=\"{k}\">{value_text(v)}</parameter>\n" for k, v in p.data.items())
            + "</invoke>"
            for p in run)
        return f"<function_calls>\n{invokes}\n</function_calls>"

    def tool_result(self, run: list[Part]) -> str:
        results = "\n".join(
            f"<result>\n<tool_name>{p.name}</tool_name>\n"
            + (f"<error>\n{result_text(p.data)}\n</error>" if p.error else f"<stdout>\n{result_text(p.data)}\n</stdout>")
            + "\n</result>"
            for p in run)
        return f"<function_results>\n{results}\n</function_results>"


class ChatML(Template):
    TOOLS_INTRO = (
        "# Tools\n\nYou may call one or more functions to assist with the user query.\n\n"
        "You are provided with function signatures within <tools></tools> XML tags:\n<tools>\n")
    TOOLS_OUTRO = (
        "\n</tools>\n\nFor each function call, return a json object with function name and arguments "
        "within <tool_call></tool_call> XML tags:\n<tool_call>\n"
        "{\"name\": <function-name>, \"arguments\": <args-json-object>}\n</tool_call>")

    def preamble(self, prompt: Prompt) -> str:
        system = self.body(prompt.system)
        if prompt.tools:
            functions = "\n".join(json.dumps({"type": "function", "function": {
                "name": t.get("name"), "description": t.get("description", ""),
                "parameters": t.get("input_schema", {})}}, ensure_ascii=False) for t in prompt.tools)
            system = "\n\n".join(s for s in (system, self.TOOLS_INTRO + functions + self.TOOLS_OUTRO) if s)
        return f"<|im_start|>system\n{system}<|im_end|>\n" if system else ""

    def turn(self, turn: Turn) -> str:
        return f"<|im_start|>{turn.role}\n{self.body(turn.parts)}<|im_end|>\n"

    def generation_prompt(self) -> str:
        return "<|im_start|>assistant\n"

    def thinking(self, p: Part) -> str:
        return f"<think>\n{p.text}\n</think>" if p.text else ""  # nothing to show without the text

    def tool_use(self, run: list[Part]) -> str:
        return "\n".join(f"<tool_call>\n{json.dumps({'name': p.name, 'arguments': p.data}, ensure_ascii=False)}\n</tool_call>"
                         for p in run)

    def tool_result(self, run: list[Part]) -> str:
        return "\n".join(f"<tool_response>\n{'[error] ' if p.error else ''}{result_text(p.data)}\n</tool_response>"
                         for p in run)


TEMPLATES = {"anthropic": AnthropicGuess, "chatml": ChatML}


# --- command --------------------------------------------------------------------------------

def message_calls(paths: list[Path]) -> list[dict]:
    return [r for r in load_records(paths)
            if r.get("path", "").split("?")[0] == "/v1/messages" and req_body(r).get("messages")]


def prompt_tokens(rec: dict) -> int | None:
    """The server's own count of the flattened prompt, from the response usage."""
    u = resp_body(rec).get("usage")
    if not u:
        return None
    return (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)


def main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser(prog="llmgrab flatten",
                                 description="Best-guess model input text for one captured Messages API call.")
    ap.add_argument("files", nargs="*", type=Path, help="capture files (default: data/cli/*.jsonl)")
    ap.add_argument("--list", action="store_true", help="list the calls with their index")
    ap.add_argument("--call", type=int, help="index of the call (negative counts from the end; default: last main call)")
    ap.add_argument("--template", choices=TEMPLATES, default="anthropic")
    ap.add_argument("--marks", action="store_true", help=f"show cache_control breakpoints as {CACHE_MARK}")
    ap.add_argument("-o", "--out", type=Path, help="write to this file (owner-only) instead of stdout")
    args = ap.parse_args(argv)

    calls = message_calls(args.files or default_paths())
    if not calls:
        sys.exit("no Messages API calls found")
    if args.list:
        for i, rec in enumerate(calls):
            req = req_body(rec)
            print(f"{i:4}  {rec.get('timestamp', '')[:19]}  {classify(rec):12} {(session_id(rec) or '-')[:8]:8} "
                  f"{len(req['messages']):4} msgs  {prompt_tokens(rec) or '?':>7} tokens  {req.get('model', '')}")
        return

    if args.call is None:
        rec = next((r for r in reversed(calls) if classify(r) == "main"), calls[-1])
    else:
        try:
            rec = calls[args.call]
        except IndexError:
            sys.exit(f"--call {args.call}: only {len(calls)} calls (see --list)")

    text = TEMPLATES[args.template](args.marks).render(parse_request(req_body(rec)))
    if args.out:
        with open_private(args.out) as f:
            f.write(text)
    else:
        sys.stdout.write(text + "\n")

    tokens = prompt_tokens(rec)
    ratio = f", {len(text) / tokens:.2f} chars/token" if tokens else ""
    print(f"[{args.template}] {len(text):,} chars; server-counted prompt: {tokens or '?'} tokens{ratio}",
          file=sys.stderr)
