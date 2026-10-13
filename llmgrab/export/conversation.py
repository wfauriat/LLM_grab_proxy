"""claude.ai data export (Settings > Privacy > Export data) -> transcript.

Prototype, not wired into `llmgrab` yet. Reads one conversation (an element of the
export's conversations.json) and writes an HTML replay, or the transcript as JSON:

    python -m llmgrab.export.conversation [data/export/conversation.json] [--json]

Export format (observed on one export, 2026-10):
  conversation  uuid, name, created_at, updated_at, chat_messages
  message       uuid, sender (human|assistant), created_at, parent_message_uuid,
                text (flattened, includes tool output), content (blocks), attachments, files
  blocks        text (+ citations), thinking (text, or hidden with summaries),
                tool_use / tool_result (same assistant message, linked by tool_use_id),
                token_budget, injected_prompt_block, document; most have start/stop_timestamp
"""

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from llmgrab import render_html
from llmgrab.paths import DATA, RENDER, open_private
from llmgrab.transcript import Block, Conversation, Message, Transcript, local_time

EXAMPLE = DATA / "export" / "conversation.json"


def seconds(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()


def thread(messages: list[dict]) -> list[dict]:
    """The branch ending at the latest message (edits and retries create other branches)."""
    by_uuid = {m["uuid"]: m for m in messages}
    msg, out = max(messages, key=lambda m: m["created_at"]), []
    while msg:
        out.append(msg)
        msg = by_uuid.get(msg["parent_message_uuid"])
    return out[::-1]


def result_text(block: dict) -> str:
    parts = []
    for item in block.get("content") or []:
        if item.get("type") == "knowledge":  # web search hit
            parts.append(f"{item.get('title')}\n{item.get('url')}\n{item.get('text') or ''}".strip())
        elif item.get("type") == "text":
            parts.append(item["text"])
        else:
            parts.append(json.dumps(item, ensure_ascii=False))
    return "\n\n".join(parts)


def citations(block: dict) -> list[str]:
    return [c["details"]["url"] for c in block.get("citations") or [] if (c.get("details") or {}).get("url")]


def assistant_blocks(content: list[dict]) -> tuple[list[Block], list[str]]:
    results = {b["tool_use_id"]: b for b in content if b["type"] == "tool_result"}
    blocks, sources = [], []
    for b in content:
        kind = b["type"]
        if kind == "text":
            if b["text"].strip():
                blocks.append(Block("text", text=b["text"]))
            sources += citations(b)
        elif kind == "thinking":
            summary = (b.get("summaries") or [{}])[-1].get("summary", "")
            if b.get("thinking"):
                blocks.append(Block("thinking", text=b["thinking"], title=summary or "Thinking"))
            else:
                blocks.append(Block("thinking", hidden=True, title=summary))
        elif kind == "tool_use":
            res = results.get(b["id"])
            status = "pending" if res is None else "err" if res.get("is_error") else "ok"
            blocks.append(Block("tool", name=b["name"], input=b.get("input"),
                                result=None if res is None else result_text(res), status=status))
        elif kind != "tool_result":
            blocks.append(Block("other", name=kind, data=b))
    if sources:
        unique = list(dict.fromkeys(sources))
        blocks.append(Block("collapsed", title=f"Sources ({len(unique)} cited)", text="\n".join(unique)))
    return blocks, [f"{len(sources)} citation{'s' * (len(sources) > 1)}"] if sources else []


def user_blocks(msg: dict) -> list[Block]:
    blocks = [Block("collapsed", title=f"attachment: {a.get('file_name')}", text=a.get("extracted_content") or "")
              for a in msg.get("attachments") or []]
    blocks += [Block("other", name=f"file: {f.get('file_name')}") for f in msg.get("files") or []]
    for b in msg["content"]:
        if b["type"] == "text":
            blocks.append(Block("text", text=b["text"].strip()))
        else:
            blocks.append(Block("collapsed", title=b["type"], text=json.dumps(b, indent=2, ensure_ascii=False)))
    return blocks


def timing(msg: dict, prompt: dict | None) -> list[str]:
    """Footer from block timestamps, relative to the prompt."""
    stamps = [b for b in msg["content"] if b.get("start_timestamp")]
    if not stamps or not prompt:
        return []
    origin = prompt["created_at"]
    first_text = next((b for b in stamps if b["type"] == "text" and b["text"].strip()), None)
    thinking = sum(seconds(b["start_timestamp"], b["stop_timestamp"]) or 0 for b in stamps if b["type"] == "thinking")
    tools = sum(b["type"] == "tool_use" for b in msg["content"])
    meta = [f"started +{seconds(origin, stamps[0]['start_timestamp']):.1f}s"]
    if thinking:
        meta.append(f"thinking {thinking:.1f}s")
    if tools:
        meta.append(f"{tools} tool calls")
    if first_text:
        meta.append(f"first text +{seconds(origin, first_text['start_timestamp']):.1f}s")
    meta.append(f"done +{seconds(origin, max(b['stop_timestamp'] or b['start_timestamp'] for b in stamps)):.1f}s")
    return meta


def build_transcript(conv: dict, source: str = "") -> Transcript:
    msgs = thread(conv["chat_messages"]) if conv["chat_messages"] else []
    items, prompt = [], None
    for msg in msgs:
        if msg["sender"] == "human":
            items.append(Message("user", user_blocks(msg), note=local_time(msg["created_at"])))
            prompt = msg
        else:
            blocks, extra = assistant_blocks(msg["content"])
            items.append(Message("assistant", blocks, reply=True, meta=timing(msg, prompt) + extra))
    branches = len(conv["chat_messages"]) - len(msgs)
    info = " · ".join(x for x in (local_time(conv["created_at"], "%Y-%m-%d %H:%M"), f"{len(msgs)} messages",
                                  f"{branches} messages on other branches" if branches else "", conv["uuid"]) if x)
    return Transcript(
        title="Export Replay",
        heading="claude.ai data export replay",
        subtitle=f"generated {datetime.now():%Y-%m-%d %H:%M} from {source} (claude.ai data export)",
        conversations=[Conversation(conv["uuid"], conv["name"] or "Untitled conversation", info, items)])


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("file", nargs="?", type=Path, default=EXAMPLE, help="one exported conversation (JSON)")
    parser.add_argument("--json", action="store_true", help="print the transcript as JSON instead of writing HTML")
    parser.add_argument("-o", "--output", type=Path, default=RENDER / "export_replay.html")
    args = parser.parse_args(argv)

    transcript = build_transcript(json.loads(args.file.read_text()), str(args.file))
    if args.json:
        json.dump(asdict(transcript), sys.stdout, indent=1, ensure_ascii=False)
        return
    page = render_html.render(transcript)
    with open_private(args.output) as f:
        f.write(page)
    print(f"wrote {args.output} ({len(page) / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
