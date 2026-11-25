#!/usr/bin/env python3
"""Wait for a task-bound project flag or read the matching completed Codex turn."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from codex_session import (
    REPORT_STATUSES, Deadline, canonical_uuid, codex_home, configure_utf8_stdio, first_line,
    local_path, message_content, project_file, project_lock, resolve_project, task_marker,
    user_message,
)
from find_codex_thread import SessionOwnershipError, require_project_session, session_by_uuid

_REPORT_FIELD = re.compile(r"^\s*(?:[-*]\s*)?\**(Status|Task[ _]ID|Round)\**\s*:\**\s*(.*?)\s*$", re.I)
_START_EVENTS = {"turn_started", "task_started"}
_END_EVENTS = {"turn_complete", "task_complete", "turn_completed"}
_FAILED_EVENTS = {"turn_aborted", "task_aborted", "turn_failed", "task_failed"}


def report_status(text: str, task_id: str, round_number: int) -> Optional[str]:
    fields = {}
    for line in text.splitlines()[:20]:
        match = _REPORT_FIELD.match(line)
        if not match:
            continue
        key = match[1].lower().replace("_", " ")
        if key in fields:
            return None
        fields[key] = match[2].strip().strip("*").strip().strip(chr(96))
    status = fields.get("status", "").upper()
    if status in REPORT_STATUSES and fields.get("task id") == task_id and fields.get("round") == str(round_number):
        return status
    return None


def emit(data: Dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    print(data["status"])
    for key, label in (
        ("elapsed", "ELAPSED_SEC"), ("task_id", "TASK_ID"), ("round", "ROUND"),
        ("thread_id", "THREAD_ID"), ("turn_id", "TURN_ID"), ("report_status", "REPORT_STATUS"),
        ("flag", "FLAG"), ("report", "REPORT"), ("note", "NOTE"),
    ):
        if key in data:
            print(f"{label}: {data[key]}")
    if "reply" in data:
        print("REPLY_BEGIN")
        print(data["reply"])
        print("REPLY_END")


def _remove(path: Path) -> str:
    try:
        path.unlink()
    except FileNotFoundError:
        return "absent"
    return "removed"


def _completion_flag(root: Path, flag: Path, report: Path, args: argparse.Namespace,
                     deadline: Deadline) -> Optional[Dict[str, Any]]:
    if not flag.is_file():
        return None
    with project_lock(root, "completion.lock", deadline):
        try:
            marker = json.loads(flag.read_text(encoding="utf-8"))
            if (not isinstance(marker, dict) or marker.get("task_id") != args.task_id
                    or type(marker.get("round")) is not int or marker["round"] != args.round):
                return None
            text = report.read_text(encoding="utf-8-sig")
        except (FileNotFoundError, json.JSONDecodeError, UnicodeError):
            return None
        status = report_status(text, args.task_id, args.round)
        if not status:
            return None
        if not args.keep_flag and _remove(flag) != "removed":
            return None
        return {"status": "CODEX_DONE", "report_status": status, "report": str(report)}


class TranscriptWatch:
    def __init__(self, directory: Path, root: Path, thread: str, marker: str,
                 turn_id: str, task_id: str, round_number: int) -> None:
        self.directory = directory
        self.root = root
        self.thread = thread
        self.marker = marker
        self.turn_id = turn_id
        self.task_id = task_id
        self.round = round_number
        self.path: Optional[Path] = None
        self.identity: Optional[Tuple[int, int]] = None
        self._reset()

    def _reset(self) -> None:
        self.offset = 0
        self.current_turn = ""
        self.target_turn = self.turn_id
        self.pending_marker = False
        self.final_reply = ""

    def _event(self, record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return None
        kind = record.get("type")
        event = payload.get("type") if kind == "event_msg" else None
        turn = payload.get("turn_id")
        if not isinstance(turn, str):
            turn = ""
        if (event in _START_EVENTS or kind == "turn_context") and turn:
            if turn != self.current_turn:
                self.final_reply = ""
            self.current_turn = turn
            if self.pending_marker and not self.turn_id:
                self.target_turn = turn
                self.pending_marker = False

        message = user_message(record)
        if message and first_line(message) == self.marker and not self.turn_id:
            self.target_turn = turn or self.current_turn
            self.pending_marker = not self.target_turn
            self.final_reply = ""

        belongs_to_target = self.target_turn and (turn or self.current_turn) == self.target_turn
        if belongs_to_target:
            if event == "agent_message" and payload.get("phase") == "final_answer":
                self.final_reply = message_content(payload.get("message"))
            elif kind == "response_item" and payload.get("role") == "assistant" and payload.get("phase") == "final_answer":
                self.final_reply = message_content(payload.get("content"))

        if event not in _END_EVENTS | _FAILED_EVENTS:
            return None
        self.current_turn = ""
        self.pending_marker = False
        if not belongs_to_target:
            self.final_reply = ""
            return None
        data = {"thread_id": self.thread, "turn_id": self.target_turn}
        if event in _FAILED_EVENTS or payload.get("status") in ("failed", "interrupted"):
            return {**data, "status": "TURN_FAILED", "note": "The matching turn did not complete normally."}
        reply = payload.get("last_agent_message") or self.final_reply
        status = report_status(reply, self.task_id, self.round) if isinstance(reply, str) else None
        if not status:
            return {**data, "status": "INVALID_REPORT",
                    "note": "The matching turn ended without a final reply for this task and round."}
        return {**data, "status": "CODEX_DONE", "report_status": status, "reply": reply}

    def poll(self) -> Optional[Dict[str, Any]]:
        if self.path is None or not self.path.is_file():
            meta = session_by_uuid(self.directory, self.thread)
            if meta is None:
                return None  # The rollout can legitimately be absent before the first turn.
            self.path = Path(require_project_session(meta, self.root)["session_file"])
            self.identity = None
        try:
            with self.path.open("rb") as stream:
                stat = os.fstat(stream.fileno())
                identity = stat.st_dev, stat.st_ino
                if identity != self.identity or stat.st_size < self.offset:
                    self._reset()
                    self.identity = identity
                    # Revalidate ownership after a rollout is atomically replaced.
                    require_project_session(session_by_uuid(self.directory, self.thread), self.root)
                stream.seek(self.offset)
                while True:
                    raw = stream.readline()
                    if not raw.endswith(b"\n"):
                        break
                    self.offset = stream.tell()
                    try:
                        record = json.loads(raw.decode("utf-8"))
                    except (UnicodeError, json.JSONDecodeError):
                        continue
                    if isinstance(record, dict):
                        result = self._event(record)
                        if result is not None:
                            return result
        except FileNotFoundError:
            self.path = None
        return None


def main() -> int:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cwd", default=os.getcwd(), help="Target project directory.")
    parser.add_argument("--mode", choices=("flag", "transcript"), default="flag")
    parser.add_argument("--path", default="", help="Flag file override, relative to --cwd or absolute.")
    parser.add_argument("--report", default="", help="Report file override (default: sibling report.md).")
    parser.add_argument("--task-id", default="", help="Unique delivery identifier. Required when waiting.")
    parser.add_argument("--round", type=int, help="Acceptance round (default 1).")
    parser.add_argument("--thread", default="", help="Thread UUID for transcript mode.")
    parser.add_argument("--turn-id", default="", help="Optional exact daemon turn ID for transcript mode.")
    parser.add_argument("--codex-home", default="", help="Codex home for transcript mode.")
    parser.add_argument("--sessions-dir", default="", help="Override the transcript directory.")
    parser.add_argument("--timeout", type=float, default=600.0, help="Maximum wait in seconds.")
    parser.add_argument("--interval", type=float, default=3.0, help="Poll interval in seconds.")
    parser.add_argument("--keep-flag", action="store_true", help="Leave a verified flag in place.")
    parser.add_argument("--clean", action="store_true", help="Remove the selected flag and report, then exit.")
    parser.add_argument("--json", action="store_true", help="Emit JSON.")
    args = parser.parse_args()

    started = time.monotonic()
    try:
        deadline = Deadline(args.timeout)
        if not math.isfinite(args.interval) or args.interval <= 0:
            raise ValueError("--interval must be finite and positive.")
        if args.round is not None and not args.task_id:
            raise ValueError("--round requires --task-id.")
        args.round = 1 if args.round is None else args.round
        if not args.clean and not args.task_id:
            raise ValueError("--task-id is required when waiting.")
        marker = task_marker(args.task_id, args.round) if args.task_id else ""
        root = resolve_project(args.cwd)
        if args.mode == "transcript":
            if args.clean or args.path or args.report or args.keep_flag:
                raise ValueError("Transcript mode cannot be combined with flag, report or cleanup options.")
            thread_id = canonical_uuid(args.thread)
            directory = local_path(args.sessions_dir, codex_home(args.codex_home) / "sessions")
            watch = TranscriptWatch(directory, root, thread_id, marker, args.turn_id, args.task_id, args.round)
        else:
            if args.thread or args.turn_id or args.codex_home or args.sessions_dir:
                raise ValueError("Thread, turn and session-store options require transcript mode.")
            if args.clean and args.keep_flag:
                raise ValueError("--clean and --keep-flag cannot be combined.")
            default_path = f"results/{args.task_id}/{args.round}/done.flag" if args.task_id else "done.flag"
            flag = project_file(root, default_path, args.path)
            report = project_file(root, "report.md", args.report or str(flag.with_name("report.md")))
            if flag == report:
                raise ValueError("Flag and report paths must be different.")
        if args.clean:
            with project_lock(root, "completion.lock", deadline):
                emit({"status": "CLEANED", "flag": _remove(flag), "report": _remove(report)}, args.json)
            return 0

        while True:
            data = watch.poll() if args.mode == "transcript" else _completion_flag(root, flag, report, args, deadline)
            if data is not None:
                data.update(task_id=args.task_id, round=args.round, elapsed=round(time.monotonic() - started, 1))
                emit(data, args.json)
                return 0 if data["status"] == "CODEX_DONE" else 1
            if not deadline.remaining():
                emit({"status": "CODEX_TIMEOUT", "task_id": args.task_id, "round": args.round,
                      "elapsed": round(time.monotonic() - started, 1)}, args.json)
                return 124
            time.sleep(min(args.interval, deadline.remaining()))
    except KeyboardInterrupt:
        emit({"status": "CODEX_INTERRUPTED"}, args.json)
        return 130
    except SessionOwnershipError as exc:
        emit({"status": "INVALID_SESSION", "note": str(exc)}, args.json)
        return 2
    except ValueError as exc:
        emit({"status": "INVALID_ARGUMENTS", "note": str(exc)}, args.json)
        return 2
    except (OSError, RuntimeError) as exc:
        emit({"status": "CLEAN_FAILED" if args.clean else "WAIT_FAILED", "note": str(exc)}, args.json)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
