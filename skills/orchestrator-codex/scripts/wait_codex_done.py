#!/usr/bin/env python3
"""Poll for Codex CLI completion signal (.agent-collab/done.flag).

Zero third-party dependencies. Compatible with Windows 11, macOS, and Linux.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from typing import Optional


def configure_utf8_stdio() -> None:
    """Ensure UTF-8 console output on Windows (avoids GBK crashes on Unicode paths)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except OSError:
                pass


def detect_report_status(report_path: Path) -> Optional[str]:
    """Read the top lines of report.md to extract a Status line if present."""
    if not report_path.is_file():
        return None
    try:
        with report_path.open("r", encoding="utf-8", errors="replace") as fh:
            for _ in range(20):
                line = fh.readline()
                if not line:
                    break
                match = re.search(
                    r"(?i)^\s*(?:[-*]\s*)?(?:\*\*)?status(?:\*\*)?\s*:\s*`?([A-Z0-9_]+)`?",
                    line,
                )
                if match:
                    return match.group(1).strip()
    except OSError:
        return None
    return None


def remove_file_safely(path: Path, retries: int = 3) -> bool:
    for attempt in range(retries):
        try:
            path.unlink(missing_ok=True)
            return True
        except OSError:
            if attempt + 1 < retries:
                time.sleep(0.1)
    return not path.exists()


def main() -> int:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(
        description="Wait for Codex CLI completion flag file and auto-clean it on success."
    )
    parser.add_argument(
        "--path",
        default=".agent-collab/done.flag",
        help="Path to completion flag file (default: .agent-collab/done.flag).",
    )
    parser.add_argument(
        "--report",
        default="",
        help="Optional path to report.md (default: report.md sibling of --path).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=600.0,
        help="Maximum seconds to wait before timing out (default: 600).",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=3.0,
        help="Polling interval in seconds (default: 3).",
    )
    parser.add_argument(
        "--keep-flag",
        action="store_true",
        help="Do not delete the flag file after detecting completion.",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Clean old done.flag and report.md before dispatch and exit immediately.",
    )
    args = parser.parse_args()

    if args.interval <= 0:
        print("ERROR: --interval must be greater than 0.", file=sys.stderr)
        return 2
    if args.timeout < 0:
        print("ERROR: --timeout must be non-negative.", file=sys.stderr)
        return 2

    flag_path = Path(args.path).expanduser()
    report_path = (
        Path(args.report).expanduser()
        if args.report
        else flag_path.parent / "report.md"
    )

    if args.clean:
        removed_flag = flag_path.exists() and remove_file_safely(flag_path)
        removed_report = report_path.exists() and remove_file_safely(report_path)
        print(
            f"CLEANED: flag={'removed' if removed_flag else 'absent'}, "
            f"report={'removed' if removed_report else 'absent'}"
        )
        return 0

    start = time.monotonic()
    deadline = start + args.timeout

    try:
        while True:
            if flag_path.exists():
                removed = True if args.keep_flag else remove_file_safely(flag_path)
                elapsed = time.monotonic() - start
                print("CODEX_DONE")
                print(f"ELAPSED_SEC: {elapsed:.1f}")
                if not args.keep_flag and not removed:
                    print(f"WARNING: Could not delete flag file: {flag_path}", file=sys.stderr)
                report_status = detect_report_status(report_path)
                if report_status:
                    print(f"REPORT_STATUS: {report_status}")
                return 0

            now = time.monotonic()
            if now >= deadline:
                elapsed = now - start
                print("CODEX_TIMEOUT")
                print(f"ELAPSED_SEC: {elapsed:.1f}")
                print(
                    "HINT: Check the Codex CLI window to see if it is waiting on a "
                    "permission approval prompt or still running a long command."
                )
                return 124

            sleep_for = min(args.interval, max(0.0, deadline - now))
            time.sleep(sleep_for)
    except KeyboardInterrupt:
        print("CODEX_INTERRUPTED", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
