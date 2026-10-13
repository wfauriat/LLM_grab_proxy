"""Replay captured conversations, from either grab path, in the terminal or as an HTML page.

Usage:
    llmgrab replay cli [data/cli/*.jsonl ...] [--session 8dc6] [--hide-side] [--html] ...
    llmgrab replay web [data/web/claude.jsonl] [--html] ...
Text goes to stdout (pipe to `less -R`); --html writes render/replay.html or
render/web_replay.html (owner-only).
"""

import argparse
import sys
from pathlib import Path

from llmgrab import render_html, render_text
from llmgrab.paths import RENDER, WEB_RECORDS, open_private
from llmgrab.records import load_records
from llmgrab.transcript import Transcript


def cli_transcript(args) -> Transcript:
    from llmgrab.cli.history import build_transcript
    from llmgrab.cli.sessions import default_paths
    return build_transcript(load_records(args.files or default_paths()), args.session, args.hide_side)


def web_transcript(args) -> Transcript:
    from llmgrab.web.timeline import build_transcript
    return build_transcript(load_records([args.file]), args.file.name)


def add_output_options(p: argparse.ArgumentParser, max_chars: int | None) -> None:
    p.add_argument("--html", action="store_true", help="write an HTML page instead of printing text")
    p.add_argument("-o", "--output", type=Path, help="HTML output path")
    p.add_argument("--max-chars", type=int, default=max_chars,
                   help=f"truncate long texts in the terminal (default: {max_chars or 'no limit'})")
    p.add_argument("--full", action="store_true", help="no truncation, panels (system prompt) in full")
    p.add_argument("--no-color", action="store_true")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="llmgrab replay", description=__doc__.splitlines()[0])
    sources = parser.add_subparsers(dest="source", required=True)

    cli = sources.add_parser("cli", help="Claude Code sessions (data/cli/*.jsonl)")
    cli.add_argument("files", nargs="*", type=Path, help="default: data/cli/*.jsonl")
    cli.add_argument("--session", help="only sessions whose id starts with this")
    cli.add_argument("--hide-side", action="store_true", help="only main-conversation calls")
    add_output_options(cli, max_chars=500)
    cli.set_defaults(build=cli_transcript, html_name="replay.html")

    web = sources.add_parser("web", help="claude.ai conversations (data/web/claude.jsonl)")
    web.add_argument("file", nargs="?", type=Path, default=WEB_RECORDS)
    add_output_options(web, max_chars=None)
    web.set_defaults(build=web_transcript, html_name="web_replay.html")
    args = parser.parse_args(argv)

    transcript = args.build(args)
    if not transcript.conversations:
        print("no matching records")
        return

    if args.html:
        out = args.output or RENDER / args.html_name
        page = render_html.render(transcript)
        with open_private(out) as f:
            f.write(page)
        print(f"wrote {out} ({len(page) / 1024:.0f} KB, {len(transcript.conversations)} conversation(s))")
    else:
        color = sys.stdout.isatty() and not args.no_color
        sys.stdout.write(render_text.render(transcript, None if args.full else args.max_chars,
                                            full_panels=args.full, color=color))


if __name__ == "__main__":
    main()
