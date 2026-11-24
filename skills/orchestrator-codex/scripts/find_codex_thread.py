#!/usr/bin/env python3
"""Auto-discover and cache the active Codex CLI thread UUID for a project root.

Zero third-party dependencies. Compatible with Windows 11, macOS, and Linux.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


def configure_utf8_stdio() -> None:
    """Ensure UTF-8 console output on Windows (avoids GBK crashes on Unicode paths)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except OSError:
                pass


def normalize_path(path_str: str) -> str:
    """Normalize a filesystem path for exact cross-style matching (Windows-safe)."""
    if not path_str:
        return ""
    s = path_str.strip()
    if s.startswith("\\\\?\\UNC\\") or s.startswith("//?/UNC/"):
        s = "\\\\" + s[8:]
    elif s.startswith("\\\\?\\") or s.startswith("//?/"):
        s = s[4:]
    s = os.path.expanduser(s)
    if os.name == "nt":
        s = s.replace("/", "\\")
        if len(s) >= 2 and s[1] == ":":
            s = s[0].lower() + s[1:]
    try:
        resolved = os.path.realpath(s)
    except OSError:
        resolved = os.path.abspath(s)
    normed = os.path.normpath(resolved)
    if os.name == "nt":
        normed = os.path.normcase(normed)
        if len(normed) > 3:
            normed = normed.rstrip("\\/")
    else:
        if len(normed) > 1:
            normed = normed.rstrip("/")
    return normed


def default_sessions_dir() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home).expanduser() / "sessions"
    return Path.home() / ".codex" / "sessions"


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def format_mtime_local(mtime: float) -> str:
    return dt.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")


def read_cache(cache_path: Path, expected_norm_cwd: str) -> Optional[Dict[str, Any]]:
    if not cache_path.is_file():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    thread_id = str(data.get("thread_id", "")).strip()
    cached_cwd = str(data.get("cwd", "")).strip()
    if not thread_id:
        return None
    if cached_cwd and normalize_path(cached_cwd) != expected_norm_cwd:
        return None
    return data


def write_cache(cache_path: Path, record: Dict[str, Any]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "thread_id": record["thread_id"],
        "cwd": record["cwd"],
        "session_file": record.get("session_file", ""),
        "session_timestamp": record.get("session_timestamp", ""),
        "originator": record.get("originator", ""),
        "source": record.get("source", ""),
        "branch": record.get("branch", ""),
        "updated_at": utc_now_iso(),
    }
    cache_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def parse_session_meta(jsonl_path: Path) -> Optional[Dict[str, Any]]:
    """Read the first line of a rollout-*.jsonl file and extract session_meta."""
    try:
        with jsonl_path.open("r", encoding="utf-8", errors="replace") as fh:
            first_line = fh.readline()
    except OSError:
        return None
    first_line = first_line.strip()
    if not first_line:
        return None
    try:
        obj = json.loads(first_line)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None

    record_type = obj.get("type")
    if record_type is not None and record_type != "session_meta":
        return None

    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else obj
    thread_id = str(
        payload.get("id")
        or payload.get("session_id")
        or obj.get("id")
        or obj.get("session_id")
        or ""
    ).strip()
    cwd = str(payload.get("cwd") or obj.get("cwd") or "").strip()
    if not cwd:
        roots = payload.get("runtime_workspace_roots")
        if isinstance(roots, list) and roots:
            cwd = str(roots[0]).strip()
    if not thread_id or not cwd:
        return None

    git_info = payload.get("git") if isinstance(payload.get("git"), dict) else {}
    timestamp = str(payload.get("timestamp") or obj.get("timestamp") or "").strip()
    raw_source = payload.get("source")
    thread_source = str(payload.get("thread_source") or "").strip().lower()
    is_subagent = (
        (isinstance(raw_source, dict) and "subagent" in raw_source)
        or thread_source == "subagent"
    )
    if isinstance(raw_source, dict):
        sub = raw_source.get("subagent")
        if isinstance(sub, dict):
            source_label = "subagent:" + ",".join(f"{k}={v}" for k, v in sub.items())
        elif sub is not None:
            source_label = f"subagent:{sub}"
        else:
            source_label = json.dumps(raw_source, ensure_ascii=False)
    else:
        source_label = str(raw_source or "")

    try:
        mtime = jsonl_path.stat().st_mtime
    except OSError:
        mtime = 0.0

    return {
        "thread_id": thread_id,
        "cwd": cwd,
        "norm_cwd": normalize_path(cwd),
        "session_timestamp": timestamp,
        "last_active_local": format_mtime_local(mtime) if mtime else "",
        "mtime": mtime,
        "originator": str(payload.get("originator") or ""),
        "source": source_label,
        "is_subagent": is_subagent,
        "branch": str(git_info.get("branch") or ""),
        "session_file": str(jsonl_path),
    }


def scan_matching_sessions(
    sessions_dir: Path,
    target_norm_cwd: str,
    include_subagents: bool = False,
) -> List[Dict[str, Any]]:
    if not sessions_dir.is_dir():
        return []

    candidates: List[tuple[float, Path]] = []
    for path in sessions_dir.rglob("rollout-*.jsonl"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        candidates.append((mtime, path))

    candidates.sort(key=lambda item: item[0], reverse=True)

    seen_ids = set()
    matches: List[Dict[str, Any]] = []
    for _, path in candidates:
        meta = parse_session_meta(path)
        if not meta:
            continue
        if not include_subagents and meta.get("is_subagent"):
            continue
        if meta["norm_cwd"] != target_norm_cwd:
            continue
        if meta["thread_id"] in seen_ids:
            continue
        seen_ids.add(meta["thread_id"])
        matches.append(meta)
    return matches


def emit_output(data: Dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return

    status = data["status"]
    print(f"STATUS: {status}")
    if data.get("thread_id"):
        print(f"THREAD_ID: {data['thread_id']}")
    if data.get("cwd"):
        print(f"CWD: {data['cwd']}")
    if data.get("last_active_local"):
        print(f"LAST_ACTIVE: {data['last_active_local']}")
    if data.get("session_timestamp"):
        print(f"SESSION_TIMESTAMP: {data['session_timestamp']}")
    if data.get("cache_file"):
        print(f"CACHE_FILE: {data['cache_file']}")
    if data.get("note"):
        print(f"NOTE: {data['note']}")

    candidates = data.get("candidates") or []
    if candidates:
        print("CANDIDATES:")
        for idx, item in enumerate(candidates, start=1):
            meta_bits = []
            if item.get("last_active_local"):
                meta_bits.append(f"active={item['last_active_local']}")
            if item.get("branch"):
                meta_bits.append(f"branch={item['branch']}")
            if item.get("originator"):
                meta_bits.append(f"origin={item['originator']}")
            if item.get("source"):
                meta_bits.append(f"source={item['source']}")
            suffix = f" ({', '.join(meta_bits)})" if meta_bits else ""
            print(f"  {idx}. {item['thread_id']}{suffix}")


def main() -> int:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(
        description="Discover and cache the active Codex CLI session UUID for a project root."
    )
    parser.add_argument(
        "--cwd",
        default=os.getcwd(),
        help="Target project root directory (default: current working directory).",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Ignore cached .agent-collab/session.json and rescan Codex sessions.",
    )
    parser.add_argument(
        "--select",
        default="",
        help="Pin a specific thread UUID into .agent-collab/session.json.",
    )
    parser.add_argument(
        "--sessions-dir",
        default="",
        help="Override Codex sessions directory (default: $CODEX_HOME/sessions or ~/.codex/sessions).",
    )
    parser.add_argument(
        "--cache-file",
        default="",
        help="Override session cache path (default: <cwd>/.agent-collab/session.json).",
    )
    parser.add_argument(
        "--recent-hours",
        type=float,
        default=24.0,
        help="Hours threshold for recent active sessions (0 disables recency filter; default: 24).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Maximum numbered candidates to show when multiple sessions match (default: 5).",
    )
    parser.add_argument(
        "--include-subagents",
        action="store_true",
        help="Include Codex internal subagent sessions (excluded by default).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output machine-readable JSON.",
    )
    args = parser.parse_args()

    target_cwd_path = Path(args.cwd).expanduser()
    try:
        resolved_cwd_display = str(target_cwd_path.resolve())
    except OSError:
        resolved_cwd_display = os.path.abspath(str(target_cwd_path))
    target_norm_cwd = normalize_path(resolved_cwd_display)

    cache_path = (
        Path(args.cache_file).expanduser()
        if args.cache_file
        else Path(resolved_cwd_display) / ".agent-collab" / "session.json"
    )
    sessions_dir = (
        Path(args.sessions_dir).expanduser() if args.sessions_dir else default_sessions_dir()
    )

    if args.select:
        selected_id = args.select.strip()
        matched_meta = None
        for item in scan_matching_sessions(sessions_dir, target_norm_cwd, include_subagents=True):
            if item["thread_id"] == selected_id:
                matched_meta = item
                break
        record = matched_meta or {
            "thread_id": selected_id,
            "cwd": resolved_cwd_display,
            "session_file": "",
            "session_timestamp": "",
            "last_active_local": "",
            "originator": "",
            "source": "",
            "branch": "",
        }
        write_cache(cache_path, record)
        emit_output(
            {
                "status": "SELECTED",
                "thread_id": record["thread_id"],
                "cwd": record["cwd"],
                "last_active_local": record.get("last_active_local", ""),
                "session_timestamp": record.get("session_timestamp", ""),
                "cache_file": str(cache_path),
            },
            args.json,
        )
        return 0

    if not args.refresh:
        cached = read_cache(cache_path, target_norm_cwd)
        if cached:
            emit_output(
                {
                    "status": "CACHED",
                    "thread_id": cached["thread_id"],
                    "cwd": cached.get("cwd", resolved_cwd_display),
                    "session_timestamp": cached.get("session_timestamp", ""),
                    "cache_file": str(cache_path),
                },
                args.json,
            )
            return 0

    matches = scan_matching_sessions(
        sessions_dir,
        target_norm_cwd,
        include_subagents=args.include_subagents,
    )
    if not matches:
        emit_output(
            {
                "status": "NOT_FOUND",
                "cwd": resolved_cwd_display,
                "sessions_dir": str(sessions_dir),
                "note": (
                    "No Codex session matched this project root. Open Codex CLI in this directory, "
                    "send a short message (e.g. 'hi') to initialize the session log, then rerun with --refresh."
                ),
            },
            args.json,
        )
        return 1

    now_ts = dt.datetime.now().timestamp()
    recent_matches = (
        [m for m in matches if (now_ts - m["mtime"]) <= args.recent_hours * 3600.0]
        if args.recent_hours > 0
        else list(matches)
    )

    if len(recent_matches) == 1:
        chosen = recent_matches[0]
        write_cache(cache_path, chosen)
        emit_output(
            {
                "status": "AUTO_SELECTED",
                "thread_id": chosen["thread_id"],
                "cwd": chosen["cwd"],
                "last_active_local": chosen["last_active_local"],
                "session_timestamp": chosen["session_timestamp"],
                "cache_file": str(cache_path),
            },
            args.json,
        )
        return 0

    pool = recent_matches if len(recent_matches) > 1 else matches
    if len(pool) == 1:
        chosen = pool[0]
        write_cache(cache_path, chosen)
        emit_output(
            {
                "status": "AUTO_SELECTED_STALE",
                "thread_id": chosen["thread_id"],
                "cwd": chosen["cwd"],
                "last_active_local": chosen["last_active_local"],
                "session_timestamp": chosen["session_timestamp"],
                "cache_file": str(cache_path),
                "note": (
                    f"Only matching session is older than {args.recent_hours:g}h. "
                    "Consider running '/new' + 'hi' in the Codex window for a fresh context."
                ),
            },
            args.json,
        )
        return 0

    limited = pool[: max(1, args.limit)]
    emit_output(
        {
            "status": "MULTIPLE_MATCHES",
            "cwd": resolved_cwd_display,
            "candidates": [
                {
                    "thread_id": m["thread_id"],
                    "last_active_local": m["last_active_local"],
                    "session_timestamp": m["session_timestamp"],
                    "branch": m["branch"],
                    "originator": m["originator"],
                    "source": m["source"],
                }
                for m in limited
            ],
            "note": "Multiple Codex sessions matched this project root. Ask the user to reply 1/2 and pin with --select <UUID>.",
        },
        args.json,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
