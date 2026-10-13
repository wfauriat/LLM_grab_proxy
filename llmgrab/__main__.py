"""llmgrab: observe what Claude clients send to and receive from Anthropic.

Usage: llmgrab <command> [args...]    (or: python -m llmgrab ...; `llmgrab <command> -h` for details)
"""

import getpass
import importlib
import os
import subprocess
import sys
from pathlib import Path

from llmgrab.paths import PCAP

PACKAGE = Path(__file__).resolve().parent
PROXY_HOST, PROXY_PORT = "127.0.0.1", 8080
UPSTREAM = "https://api.anthropic.com"


def proxy(argv: list[str]) -> None:
    """Reverse proxy to the Anthropic API, logging to data/cli/ (extra args go to mitmdump)."""
    mitmdump = Path(sys.executable).parent / "mitmdump"
    os.execv(mitmdump, [str(mitmdump), "--mode", f"reverse:{UPSTREAM}", "--listen-host", PROXY_HOST,
                        "-p", str(PROXY_PORT), "-s", str(PACKAGE / "cli" / "addon.py"), *argv])


def claude(argv: list[str]) -> None:
    """Start Claude Code through the proxy (extra args go to claude)."""
    env = {**os.environ, "ANTHROPIC_BASE_URL": f"http://{PROXY_HOST}:{PROXY_PORT}"}
    os.execvpe("claude", ["claude", *argv], env)


def browser(argv: list[str]) -> None:
    """Dedicated Firefox profile logging its TLS keys to data/web/keys.log."""
    script = PACKAGE / "web" / "launch_firefox.sh"
    os.execv("/bin/bash", ["bash", str(script), *argv])


def tcpdump(argv: list[str]) -> None:
    """Print the packet-capture command to run from a sudo-capable shell."""
    route = subprocess.run(["ip", "route", "get", "1.1.1.1"], capture_output=True, text=True).stdout.split()
    iface = route[route.index("dev") + 1] if "dev" in route else "<iface>"
    print(f"sudo tcpdump -i {iface} -U -Z {getpass.getuser()} -w {PCAP} 'tcp port 443'")


def jq(argv: list[str]) -> None:
    """jq recipes over the CLI captures (no args: list them)."""
    os.execv("/bin/bash", ["bash", str(PACKAGE / "cli" / "jqx.sh"), *argv])


# name -> function, or "module:function" imported on demand (keeps heavy imports lazy)
COMMANDS = {
    "proxy": proxy,
    "claude": claude,
    "replay": "llmgrab.replay:main",
    "analyze": "llmgrab.analyze:main",
    "jq": jq,
    "flatten": "llmgrab.cli.flatten:main",
    "browser": browser,
    "tcpdump": tcpdump,
    "decode": "llmgrab.web.pcap:main",
    "clean": "llmgrab.web.clean:main",
}
HELP = {
    "replay": "Replay conversations: `replay cli|web [--html]` (terminal, or render/*.html)",
    "analyze": "Report: `analyze cli|web|all` (calls/turns, conversations, endpoints, timings)",
    "flatten": "Best-guess model input text for one captured call (--list, --call N, --template)",
    "decode": "Decrypt data/web/claude.pcap + keys.log into data/web/claude.jsonl",
    "clean": "After decode: delete pcap, keys.log and Firefox profile (asks first; --keep-profile)",
}


def usage() -> None:
    print(__doc__)
    for group, names in (("CLI grab (Claude Code via proxy)", ["proxy", "claude", "jq", "flatten"]),
                         ("Web grab (claude.ai via passive capture)", ["browser", "tcpdump", "decode", "clean"]),
                         ("Both", ["replay", "analyze"])):
        print(f"{group}:")
        for name in names:
            target = COMMANDS[name]
            doc = HELP.get(name) or (target.__doc__ or "").strip()
            print(f"  {name:12} {doc}")
        print()


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        usage()
        return
    name, argv = sys.argv[1], sys.argv[2:]
    target = COMMANDS.get(name)
    if target is None:
        print(f"unknown command: {name}\n")
        usage()
        sys.exit(2)
    if isinstance(target, str):
        module, func = target.split(":")
        target = getattr(importlib.import_module(module), func)
    target(argv)


if __name__ == "__main__":
    main()
