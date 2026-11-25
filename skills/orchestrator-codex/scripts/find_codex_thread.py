#!/usr/bin/env python3
"""Auto-discover and cache the active Codex CLI thread UUID for a project root.

Supports:
- Cached binding lookup with SANDBOX_BACKGROUND live PID + name + creation-time fingerprint verification
  (returns SANDBOX_SESSION_OFFLINE when background process has exited instead of returning stale CACHED).
- --since <timestamp> creation-time filtering for capturing newly spawned windows.
- --list-history for on-demand historical session selection prior to `start_codex_window.py --resume`.

Zero third-party dependencies. Compatible with Python 3.8+ on Windows 11, macOS, and Linux.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HARNESS_TEXT_PREFIXES = (
    "# agents.md instructions",
    "<environment_context>",
    "<instructions>",
    "<user_instructions>",
    "<permissions instructions>",
    "<collaboration_mode>",
    "<skills_instructions>",
    "<turn_aborted>",
)

ALLOWED_PID_NAMES_WIN = frozenset(("conhost.exe", "powershell.exe", "codex.exe"))
ALLOWED_PID_NAMES_POSIX = frozenset(("codex", "bash", "zsh", "sh"))
MODE_SANDBOX_BACKGROUND = "SANDBOX_BACKGROUND"

_FILETIME_UNIX_EPOCH_TICKS = 11644473600 * 10_000_000
_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


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


def filetime_to_iso_utc(filetime_ticks: int) -> str:
    """Lossless 100ns UTC ISO-8601 fingerprint from Win32 FILETIME."""
    unix_100ns = filetime_ticks - _FILETIME_UNIX_EPOCH_TICKS
    if unix_100ns < 0:
        return f"ft:{filetime_ticks}"
    secs, rem_100ns = divmod(unix_100ns, 10_000_000)
    base = dt.datetime.fromtimestamp(secs, tz=dt.timezone.utc)
    return f"{base.strftime('%Y-%m-%dT%H:%M:%S')}.{rem_100ns:07d}Z"


def get_process_identity(pid: int) -> Optional[Dict[str, Any]]:
    """Query live process identity (pid, pid_name, pid_created_at)."""
    if not isinstance(pid, int) or pid <= 0:
        return None

    if os.name == "nt":
        try:
            import ctypes.wintypes as wt

            k32 = ctypes.windll.kernel32
            k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
            k32.OpenProcess.restype = wt.HANDLE
            k32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
            k32.GetExitCodeProcess.restype = wt.BOOL
            k32.GetProcessTimes.argtypes = [
                wt.HANDLE,
                ctypes.POINTER(wt.FILETIME),
                ctypes.POINTER(wt.FILETIME),
                ctypes.POINTER(wt.FILETIME),
                ctypes.POINTER(wt.FILETIME),
            ]
            k32.GetProcessTimes.restype = wt.BOOL
            k32.QueryFullProcessImageNameW.argtypes = [
                wt.HANDLE,
                wt.DWORD,
                wt.LPWSTR,
                ctypes.POINTER(wt.DWORD),
            ]
            k32.QueryFullProcessImageNameW.restype = wt.BOOL
            k32.CloseHandle.argtypes = [wt.HANDLE]
            k32.CloseHandle.restype = wt.BOOL

            hproc = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not hproc:
                return None
            try:
                exit_code = wt.DWORD(0)
                if not k32.GetExitCodeProcess(hproc, ctypes.byref(exit_code)):
                    return None
                if exit_code.value != _STILL_ACTIVE:
                    return None

                ct, et, kt, ut = wt.FILETIME(), wt.FILETIME(), wt.FILETIME(), wt.FILETIME()
                if not k32.GetProcessTimes(
                    hproc, ctypes.byref(ct), ctypes.byref(et), ctypes.byref(kt), ctypes.byref(ut)
                ):
                    return None
                ft_ticks = (ct.dwHighDateTime << 32) | ct.dwLowDateTime

                nlen = wt.DWORD(1024)
                nbuf = ctypes.create_unicode_buffer(1024)
                if not k32.QueryFullProcessImageNameW(hproc, 0, nbuf, ctypes.byref(nlen)):
                    return None
                exe_name = os.path.basename(nbuf.value).strip()
                if not exe_name:
                    return None

                return {
                    "pid": pid,
                    "pid_name": exe_name,
                    "pid_created_at": filetime_to_iso_utc(ft_ticks),
                }
            finally:
                k32.CloseHandle(hproc)
        except (AttributeError, OSError, ValueError):
            return None

    try:
        os.kill(pid, 0)
    except OSError:
        return None
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm=,lstart="],
            capture_output=True,
            text=True,
            check=False,
        )
        line = proc.stdout.strip()
        if not line:
            return None
        parts = line.split(None, 1)
        comm = os.path.basename(parts[0]).strip()
        lstart = parts[1].strip() if len(parts) > 1 else ""
        return {
            "pid": pid,
            "pid_name": comm,
            "pid_created_at": lstart,
        }
    except OSError:
        return None


def verify_managed_process(
    pid: Any,
    expected_name: str,
    expected_created_at: str,
) -> Tuple[bool, str]:
    """Strictly verify PID + allowlisted process name + creation-time fingerprint."""
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False, "invalid_or_missing_pid"
    if pid_int <= 0:
        return False, "invalid_or_missing_pid"

    exp_name = str(expected_name or "").strip().lower()
    exp_created = str(expected_created_at or "").strip()
    allowed = ALLOWED_PID_NAMES_WIN if os.name == "nt" else ALLOWED_PID_NAMES_POSIX
    if not exp_name or exp_name not in allowed:
        return False, "disallowed_pid_name"
    if not exp_created:
        return False, "missing_pid_created_at"

    live = get_process_identity(pid_int)
    if not live:
        return False, "process_not_running"

    live_name = str(live.get("pid_name") or "").strip().lower()
    live_created = str(live.get("pid_created_at") or "").strip()
    if live_name not in allowed or live_name != exp_name:
        return False, "pid_name_mismatch"
    if live_created != exp_created:
        return False, "pid_created_at_mismatch"
    return True, "verified_alive"


def parse_timestamp(value: Any) -> Optional[float]:
    """Parse Unix epoch seconds (int/float/str) or ISO-8601 string (Python 3.8+ safe)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass

    iso = text
    if iso.endswith(("Z", "z")):
        iso = iso[:-1] + "+00:00"
    iso = re.sub(
        r"\.(\d+)",
        lambda m: "." + (m.group(1)[:6]).ljust(6, "0"),
        iso,
        count=1,
    )
    try:
        parsed = dt.datetime.fromisoformat(iso)
    except ValueError:
        return None
    return parsed.timestamp()


def summarize_text(text: str, max_len: int = 60) -> str:
    compact = " ".join(text.split())
    if not compact:
        return ""
    if len(compact) <= max_len:
        return compact
    return compact[: max(1, max_len - 3)].rstrip() + "..."


def is_harness_injected_text(text: str) -> bool:
    stripped = text.strip().lower()
    if not stripped:
        return True
    return any(stripped.startswith(prefix) for prefix in HARNESS_TEXT_PREFIXES)


def read_cache(cache_path: Path, expected_norm_cwd: str) -> Optional[Dict[str, Any]]:
    try:
        if not cache_path.is_file():
            return None
        data = json.loads(cache_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    cached_cwd = str(data.get("cwd", "")).strip()
    if cached_cwd and normalize_path(cached_cwd) != expected_norm_cwd:
        return None
    thread_id = str(data.get("thread_id", "")).strip()
    mode = str(data.get("mode", "")).strip().upper()
    if not thread_id and mode != MODE_SANDBOX_BACKGROUND:
        return None
    return data


def write_cache(cache_path: Path, record: Dict[str, Any]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload: Dict[str, Any] = {
        "thread_id": record["thread_id"],
        "cwd": record["cwd"],
        "session_file": record.get("session_file", ""),
        "session_timestamp": record.get("session_timestamp", ""),
        "originator": record.get("originator", ""),
        "source": record.get("source", ""),
        "branch": record.get("branch", ""),
        "updated_at": utc_now_iso(),
    }
    for extra_key in (
        "model",
        "reasoning_effort",
        "sandbox",
        "approval",
        "mode",
        "desktop",
        "pid",
        "pid_name",
        "pid_created_at",
        "launched_by",
    ):
        if record.get(extra_key) not in (None, ""):
            payload[extra_key] = record[extra_key]
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
        or thread_source in ("subagent", "guardian_review")
        or bool(payload.get("parent_thread_id"))
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

    initial_model = str(payload.get("model") or "").strip()
    if not initial_model:
        base_inst = payload.get("base_instructions")
        if isinstance(base_inst, dict):
            prov = base_inst.get("provenance")
            if isinstance(prov, dict):
                initial_model = str(prov.get("model") or "").strip()

    try:
        st = jsonl_path.stat()
        mtime = st.st_mtime
        ctime = st.st_ctime
    except OSError:
        mtime = 0.0
        ctime = 0.0

    created_ts = parse_timestamp(timestamp)
    if created_ts is None:
        created_ts = parse_timestamp(obj.get("timestamp"))
    if created_ts is None:
        created_ts = ctime or mtime

    return {
        "thread_id": thread_id,
        "cwd": cwd,
        "norm_cwd": normalize_path(cwd),
        "session_timestamp": timestamp,
        "created_ts": created_ts,
        "last_active_local": format_mtime_local(mtime) if mtime else "",
        "mtime": mtime,
        "originator": str(payload.get("originator") or ""),
        "source": source_label,
        "is_subagent": is_subagent,
        "branch": str(git_info.get("branch") or ""),
        "model": initial_model,
        "reasoning_effort": "",
        "first_user_message": "",
        "session_file": str(jsonl_path),
    }


def _extract_user_text_from_content(
    content: Any,
    content_item_kinds: Optional[List[Any]] = None,
) -> str:
    if isinstance(content, str):
        return "" if is_harness_injected_text(content) else content
    if not isinstance(content, list):
        return ""

    parts: List[str] = []
    for idx, block in enumerate(content):
        if content_item_kinds and idx < len(content_item_kinds):
            kind = str(content_item_kinds[idx] or "").strip().lower()
            if kind and not kind.startswith("user"):
                continue
        text = ""
        if isinstance(block, str):
            text = block
        elif isinstance(block, dict):
            raw = block.get("text")
            if isinstance(raw, str):
                text = raw
        if text and not is_harness_injected_text(text):
            parts.append(text)
    return " ".join(parts).strip()


def extract_session_details(jsonl_path: Path, fallback_model: str = "") -> Dict[str, str]:
    """Scan a rollout-*.jsonl file to extract latest model, effort, and first user message."""
    model = fallback_model.strip()
    effort = ""
    first_user_message = ""

    try:
        fh = jsonl_path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return {
            "model": model,
            "reasoning_effort": effort,
            "first_user_message": first_user_message,
        }

    with fh:
        for line in fh:
            if not line or line[0] != "{":
                continue

            want_model = (
                "turn_context" in line
                or "thread_settings_applied" in line
                or "world_state" in line
            )
            want_user = not first_user_message and (
                "UserMessage" in line
                or "user_message" in line
                or '"user"' in line
            )
            if not want_model and not want_user:
                continue

            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue

            rec_type = obj.get("type")
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                continue

            if rec_type == "turn_context":
                cand_model = str(payload.get("model") or "").strip()
                collab = payload.get("collaboration_mode")
                collab_settings = collab.get("settings") if isinstance(collab, dict) else None
                if not cand_model and isinstance(collab_settings, dict):
                    cand_model = str(collab_settings.get("model") or "").strip()
                if cand_model:
                    model = cand_model

                cand_effort = str(
                    payload.get("effort")
                    or payload.get("reasoning_effort")
                    or (
                        collab_settings.get("reasoning_effort")
                        if isinstance(collab_settings, dict)
                        else ""
                    )
                    or ""
                ).strip()
                if cand_effort:
                    effort = cand_effort

            elif rec_type == "event_msg":
                ev_type = payload.get("type")
                if ev_type == "thread_settings_applied":
                    settings = payload.get("thread_settings")
                    if isinstance(settings, dict):
                        cand_model = str(settings.get("model") or "").strip()
                        collab = settings.get("collaboration_mode")
                        collab_settings = collab.get("settings") if isinstance(collab, dict) else None
                        if not cand_model and isinstance(collab_settings, dict):
                            cand_model = str(collab_settings.get("model") or "").strip()
                        if cand_model:
                            model = cand_model
                        cand_effort = str(
                            settings.get("reasoning_effort")
                            or (
                                collab_settings.get("reasoning_effort")
                                if isinstance(collab_settings, dict)
                                else ""
                            )
                            or ""
                        ).strip()
                        if cand_effort:
                            effort = cand_effort

                elif not first_user_message and ev_type == "user_message":
                    raw_msg = payload.get("message")
                    if isinstance(raw_msg, str) and not is_harness_injected_text(raw_msg):
                        first_user_message = summarize_text(raw_msg)

                elif not first_user_message and ev_type == "item_completed":
                    item = payload.get("item")
                    if isinstance(item, dict) and item.get("type") == "UserMessage":
                        extracted = _extract_user_text_from_content(item.get("content"))
                        if extracted:
                            first_user_message = summarize_text(extracted)

            elif rec_type == "world_state":
                state = payload.get("state")
                if isinstance(state, dict):
                    model_obj = state.get("model")
                    if isinstance(model_obj, dict) and model_obj.get("model"):
                        model = str(model_obj["model"]).strip()
                    else:
                        collab = state.get("collaboration_mode")
                        if isinstance(collab, dict) and collab.get("model"):
                            model = str(collab["model"]).strip()

            elif (
                not first_user_message
                and rec_type == "response_item"
                and payload.get("type") == "message"
                and payload.get("role") == "user"
            ):
                passthrough = payload.get("internal_chat_message_metadata_passthrough")
                kinds = (
                    passthrough.get("content_item_kinds")
                    if isinstance(passthrough, dict) and isinstance(passthrough.get("content_item_kinds"), list)
                    else None
                )
                extracted = _extract_user_text_from_content(payload.get("content"), kinds)
                if extracted:
                    first_user_message = summarize_text(extracted)

    return {
        "model": model,
        "reasoning_effort": effort,
        "first_user_message": first_user_message,
    }


def enrich_session(record: Dict[str, Any]) -> Dict[str, Any]:
    session_file = record.get("session_file")
    if not session_file:
        return record
    details = extract_session_details(Path(session_file), fallback_model=record.get("model", ""))
    record["model"] = details["model"]
    record["reasoning_effort"] = details["reasoning_effort"]
    record["first_user_message"] = details["first_user_message"]
    return record


def scan_matching_sessions(
    sessions_dir: Path,
    target_norm_cwd: str,
    include_subagents: bool = False,
    since_ts: Optional[float] = None,
) -> List[Dict[str, Any]]:
    try:
        if not sessions_dir.is_dir():
            return []
    except OSError:
        return []

    candidates: List[Tuple[float, Path]] = []
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
        if since_ts is not None and meta["created_ts"] < since_ts:
            continue
        if meta["thread_id"] in seen_ids:
            continue
        seen_ids.add(meta["thread_id"])
        matches.append(meta)

    if since_ts is not None:
        matches.sort(key=lambda m: (m["created_ts"], m["mtime"]), reverse=True)
    return matches


def serialize_candidate(m: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "thread_id": m["thread_id"],
        "short_id": m["thread_id"][:8],
        "model": m.get("model", ""),
        "reasoning_effort": m.get("reasoning_effort", ""),
        "last_active_local": m["last_active_local"],
        "session_timestamp": m["session_timestamp"],
        "first_user_message": m.get("first_user_message", ""),
        "branch": m["branch"],
        "originator": m["originator"],
        "source": m["source"],
        "session_file": m.get("session_file", ""),
    }


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
    if data.get("mode"):
        print(f"MODE: {data['mode']}")
    if data.get("pid") not in (None, ""):
        print(f"PID: {data['pid']}")
    if data.get("pid_name"):
        print(f"PID_NAME: {data['pid_name']}")
    if data.get("offline_reason"):
        print(f"OFFLINE_REASON: {data['offline_reason']}")
    if data.get("model"):
        print(f"MODEL: {data['model']}")
    if data.get("reasoning_effort"):
        print(f"EFFORT: {data['reasoning_effort']}")
    if data.get("last_active_local"):
        print(f"LAST_ACTIVE: {data['last_active_local']}")
    if data.get("session_timestamp"):
        print(f"SESSION_TIMESTAMP: {data['session_timestamp']}")
    if data.get("cache_file"):
        print(f"CACHE_FILE: {data['cache_file']}")
    if data.get("note"):
        print(f"NOTE: {data['note']}")

    entries = data.get("history") or data.get("candidates") or []
    if entries:
        header = "HISTORY:" if data.get("history") else "CANDIDATES:"
        print(header)
        for idx, item in enumerate(entries, start=1):
            short_id = item.get("short_id") or item["thread_id"][:8]
            meta_bits = []
            model_val = item.get("model") or "unknown"
            effort_val = item.get("reasoning_effort") or "unknown"
            meta_bits.append(f"model={model_val}")
            meta_bits.append(f"effort={effort_val}")
            if item.get("last_active_local"):
                meta_bits.append(f"active={item['last_active_local']}")
            if item.get("first_user_message"):
                meta_bits.append(f'first="{item["first_user_message"]}"')
            if item.get("branch"):
                meta_bits.append(f"branch={item['branch']}")
            suffix = f" ({', '.join(meta_bits)})" if meta_bits else ""
            print(f"  {idx}. {item['thread_id']} [{short_id}]{suffix}")


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
        "--list-history",
        action="store_true",
        help="List all historical Codex sessions for --cwd (for on-demand --resume selection).",
    )
    parser.add_argument(
        "--since",
        default="",
        help="Only match sessions created at or after this Unix epoch or ISO-8601 timestamp.",
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
        default=0,
        help="Maximum items to show (default: 5 for MULTIPLE_MATCHES, 10 for --list-history).",
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

    since_ts: Optional[float] = None
    if args.since:
        since_ts = parse_timestamp(args.since)
        if since_ts is None:
            print(
                f"ERROR: Invalid --since timestamp '{args.since}'. "
                "Provide Unix epoch seconds (e.g. 1760000000.5) or ISO-8601 (e.g. 2026-11-24T14:33:33Z).",
                file=sys.stderr,
            )
            return 2

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

    if args.list_history:
        history_matches = scan_matching_sessions(
            sessions_dir,
            target_norm_cwd,
            include_subagents=args.include_subagents,
            since_ts=since_ts,
        )
        if not history_matches:
            emit_output(
                {
                    "status": "NOT_FOUND",
                    "cwd": resolved_cwd_display,
                    "sessions_dir": str(sessions_dir),
                    "note": "No historical Codex sessions found for this project root.",
                },
                args.json,
            )
            return 1

        max_items = args.limit if args.limit > 0 else 10
        enriched = [enrich_session(m) for m in history_matches[:max_items]]
        emit_output(
            {
                "status": "HISTORY_LISTED",
                "cwd": resolved_cwd_display,
                "total": len(history_matches),
                "history": [serialize_candidate(m) for m in enriched],
                "note": (
                    "Present these historical sessions to the user ONLY when they explicitly asked to resume. "
                    "After the user selects one and specifies model & effort, launch via "
                    "start_codex_window.py --resume <UUID>."
                ),
            },
            args.json,
        )
        return 0

    if args.select:
        selected_id = args.select.strip()
        matched_meta = None
        for item in scan_matching_sessions(sessions_dir, target_norm_cwd, include_subagents=True):
            if item["thread_id"] == selected_id:
                matched_meta = enrich_session(item)
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
            "model": "",
            "reasoning_effort": "",
        }
        write_cache(cache_path, record)
        emit_output(
            {
                "status": "SELECTED",
                "thread_id": record["thread_id"],
                "cwd": record["cwd"],
                "model": record.get("model", ""),
                "reasoning_effort": record.get("reasoning_effort", ""),
                "last_active_local": record.get("last_active_local", ""),
                "session_timestamp": record.get("session_timestamp", ""),
                "cache_file": str(cache_path),
            },
            args.json,
        )
        return 0

    if not args.refresh and since_ts is None:
        cached = read_cache(cache_path, target_norm_cwd)
        if cached:
            cached_mode = str(cached.get("mode") or "").strip().upper()
            if cached_mode == MODE_SANDBOX_BACKGROUND:
                ok_alive, reason = verify_managed_process(
                    cached.get("pid"),
                    str(cached.get("pid_name") or ""),
                    str(cached.get("pid_created_at") or ""),
                )
                if not ok_alive:
                    # Strictly report offline; NEVER return CACHED or fall back to AUTO_SELECTED
                    emit_output(
                        {
                            "status": "SANDBOX_SESSION_OFFLINE",
                            "thread_id": cached.get("thread_id", ""),
                            "cwd": cached.get("cwd", resolved_cwd_display),
                            "mode": MODE_SANDBOX_BACKGROUND,
                            "pid": cached.get("pid"),
                            "pid_name": cached.get("pid_name", ""),
                            "offline_reason": reason,
                            "model": cached.get("model", ""),
                            "reasoning_effort": cached.get("reasoning_effort", ""),
                            "cache_file": str(cache_path),
                            "note": (
                                "Cached SANDBOX_BACKGROUND Codex process is offline or PID fingerprint mismatch. "
                                "Do NOT dispatch to this offline thread and do NOT auto-select stale logs; "
                                "start a fresh session via start_codex_window.py (after user specifies model & effort), "
                                "or resume only if the user explicitly asks to continue history."
                            ),
                        },
                        args.json,
                    )
                    return 3

            if cached.get("thread_id"):
                emit_output(
                    {
                        "status": "CACHED",
                        "thread_id": cached["thread_id"],
                        "cwd": cached.get("cwd", resolved_cwd_display),
                        "mode": cached_mode,
                        "pid": cached.get("pid"),
                        "pid_name": cached.get("pid_name", ""),
                        "model": cached.get("model", ""),
                        "reasoning_effort": cached.get("reasoning_effort", ""),
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
        since_ts=since_ts,
    )
    if not matches:
        note = (
            "No Codex session created since the requested --since timestamp matched this project root."
            if since_ts is not None
            else (
                "No Codex session matched this project root. Launch a new Codex session via "
                "start_codex_window.py (after confirming model & effort with the user)."
            )
        )
        emit_output(
            {
                "status": "NOT_FOUND",
                "cwd": resolved_cwd_display,
                "sessions_dir": str(sessions_dir),
                "note": note,
            },
            args.json,
        )
        return 1

    if since_ts is not None:
        recent_matches = list(matches)
    else:
        now_ts = dt.datetime.now().timestamp()
        recent_matches = (
            [m for m in matches if (now_ts - m["mtime"]) <= args.recent_hours * 3600.0]
            if args.recent_hours > 0
            else list(matches)
        )

    if len(recent_matches) == 1:
        chosen = enrich_session(recent_matches[0])
        write_cache(cache_path, chosen)
        emit_output(
            {
                "status": "AUTO_SELECTED",
                "thread_id": chosen["thread_id"],
                "cwd": chosen["cwd"],
                "model": chosen.get("model", ""),
                "reasoning_effort": chosen.get("reasoning_effort", ""),
                "last_active_local": chosen["last_active_local"],
                "session_timestamp": chosen["session_timestamp"],
                "cache_file": str(cache_path),
            },
            args.json,
        )
        return 0

    pool = recent_matches if len(recent_matches) > 1 else matches
    if len(pool) == 1:
        chosen = enrich_session(pool[0])
        write_cache(cache_path, chosen)
        emit_output(
            {
                "status": "AUTO_SELECTED_STALE",
                "thread_id": chosen["thread_id"],
                "cwd": chosen["cwd"],
                "model": chosen.get("model", ""),
                "reasoning_effort": chosen.get("reasoning_effort", ""),
                "last_active_local": chosen["last_active_local"],
                "session_timestamp": chosen["session_timestamp"],
                "cache_file": str(cache_path),
                "note": (
                    f"Only matching session is older than {args.recent_hours:g}h. "
                    "Consider running '/new' + 'hi' in the Codex window or opening a new session."
                ),
            },
            args.json,
        )
        return 0

    max_candidates = args.limit if args.limit > 0 else 5
    limited = [enrich_session(m) for m in pool[:max_candidates]]
    emit_output(
        {
            "status": "MULTIPLE_MATCHES",
            "cwd": resolved_cwd_display,
            "candidates": [serialize_candidate(m) for m in limited],
            "note": (
                "Multiple Codex sessions matched this project root. Present numbered candidates "
                "(UUID short code, model, reasoning effort, last active time, first message summary) plus option [0] "
                "to launch a new Codex session, then pin via --select <UUID> or run start_codex_window.py --force-new."
            ),
        },
        args.json,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
