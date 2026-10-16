"""Project locations and owner-only file writing (captures hold prompts and personal data)."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RENDER = ROOT / "render"

# Path 1: Claude Code through the mitmproxy reverse proxy.
# One JSONL file per day; LLMGRAB_CLI_LOGS moves it, e.g. into the project being worked on.
CLI_LOGS = Path(os.environ["LLMGRAB_CLI_LOGS"]) if os.environ.get("LLMGRAB_CLI_LOGS") else DATA / "cli"

# Path 2: passive browser capture.
WEB = DATA / "web"
PCAP = WEB / "claude.pcap"
KEYLOG = WEB / "keys.log"
WEB_RECORDS = WEB / "claude.jsonl"
FIREFOX_PROFILE = WEB / "firefox-profile"


def private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def open_private(path: Path, mode: str = "w"):
    """Text file created with 0o600; mode "w" truncates, "a" appends."""
    private_dir(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if mode == "a" else os.O_TRUNC)
    return os.fdopen(os.open(path, flags, 0o600), mode, encoding="utf-8")
