"""Delete the raw web-grab data once it has been decoded: packet capture, TLS key log, Firefox profile.

Keeps data/web/claude.jsonl (what the replays use). Refuses while tcpdump or the
capture Firefox is running, or when the capture has not been decoded since it was made.

Usage:
    llmgrab clean [--keep-profile] [--yes] [--force]
"""

import argparse
import shutil
import subprocess
from pathlib import Path

from llmgrab.paths import FIREFOX_PROFILE, KEYLOG, PCAP, WEB_RECORDS


def size(path: Path) -> int:
    if path.is_dir():
        return sum(p.stat().st_size for p in path.rglob("*") if p.is_file() and not p.is_symlink())
    return path.stat().st_size


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def running(pattern: str) -> list[str]:
    """Command lines of processes matching `pattern` (pgrep -f), excluding pgrep itself."""
    out = subprocess.run(["pgrep", "-af", pattern], capture_output=True, text=True).stdout
    return [line for line in out.splitlines() if "pgrep" not in line]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="llmgrab clean", description=__doc__.splitlines()[0])
    parser.add_argument("--keep-profile", action="store_true",
                        help="keep the Firefox profile (stay logged in for the next capture)")
    parser.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    parser.add_argument("--force", action="store_true", help="delete even if the capture was not decoded")
    args = parser.parse_args(argv)

    targets = [p for p in (PCAP, KEYLOG) if p.exists()]
    if not args.keep_profile and FIREFOX_PROFILE.exists():
        targets.append(FIREFOX_PROFILE)
    if not targets:
        print("nothing to clean")
        return

    busy = running(f"tcpdump.*{PCAP}") + running(f"firefox.*{FIREFOX_PROFILE}")
    if busy:
        print("refusing: capture still running (stop tcpdump / close that Firefox first):")
        print("\n".join(f"  {line[:150]}" for line in busy))
        raise SystemExit(1)

    if PCAP.exists() and not args.force:
        if not WEB_RECORDS.exists():
            print(f"refusing: {WEB_RECORDS.name} not found, run `llmgrab decode` first (or --force)")
            raise SystemExit(1)
        if WEB_RECORDS.stat().st_mtime < PCAP.stat().st_mtime:
            print(f"refusing: {PCAP.name} is newer than {WEB_RECORDS.name}, run `llmgrab decode` again (or --force)")
            raise SystemExit(1)

    print("This permanently deletes:")
    for path in targets:
        print(f"  {human(size(path)):>9}  {path}")
    print(f"Kept: {WEB_RECORDS}" if WEB_RECORDS.exists() else "")
    print("Warning: without the pcap and keys, this capture can't be decoded again (e.g. by an\n"
          "improved decoder).")
    if FIREFOX_PROFILE in targets:
        print("Warning: deleting the profile does not log you out; the claude.ai session stays valid\n"
              "on the server. Log out in that Firefox window first (or use --keep-profile).")
    if not args.yes:
        try:
            answer = input("Delete? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("cancelled")
            return

    for path in targets:
        shutil.rmtree(path) if path.is_dir() else path.unlink()
    print(f"deleted {len(targets)} item(s)")


if __name__ == "__main__":
    main()
