#!/usr/bin/env python3
"""Find, verify, and pin a Codex thread for a specific project."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from codex_daemon_rpc import DaemonRPCError, ThreadOwnershipError, check_daemon_thread_loaded
from codex_session import (
    BOOTSTRAP_PREFIX, MODE_DAEMON_MANAGED, MODE_SANDBOX_BACKGROUND, MODE_VISIBLE_WINDOW,
    CodexCLI, Deadline, atomic_json, canonical_uuid, codex_home, configure_utf8_stdio,
    first_line, has_active_writer, local_path, normalize_path, project_file, project_lock,
    read_json, resolve_project, same_project, user_message,
)
from codex_windows import legacy_process_state

HARNESS_TEXT_PREFIXES = (
    "# agents.md instructions",
    "<environment_context>",
    "<instructions>",
    "<user_instructions>",
    "<permissions instructions>",
    "<collaboration_mode>",
    "<skills_instructions>",
    "<turn_aborted>",
    BOOTSTRAP_PREFIX,
)
MANAGED_MODES = {MODE_DAEMON_MANAGED, MODE_SANDBOX_BACKGROUND}


class SessionOwnershipError(ValueError):
    """A requested binding or rollout cannot be attributed to this project."""


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def format_mtime_local(mtime: float) -> str:
    return dt.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")


def parse_timestamp(value: Any) -> Optional[float]:
    """Parse Unix epoch seconds (int/float/str) or ISO-8601 string (Python 3.8+ safe)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    text = str(value).strip()
    if not text:
        return None
    try:
        number = float(text)
        return number if math.isfinite(number) else None
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
    cwd = str(payload.get("cwd") or obj.get("cwd") or "")
    if not cwd:
        roots = payload.get("runtime_workspace_roots")
        if isinstance(roots, list) and roots:
            cwd = str(roots[0])
    try:
        thread_id = canonical_uuid(thread_id)
        if not cwd or not Path(cwd).is_absolute():
            return None
        normalized_cwd = normalize_path(cwd)
    except (ValueError, OSError, RuntimeError):
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
        "norm_cwd": normalized_cwd,
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

    def substantive(text: str) -> bool:
        return not is_harness_injected_text(text) and text.strip().lower() != "hi"

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
                    if isinstance(raw_msg, str) and substantive(raw_msg):
                        first_user_message = summarize_text(raw_msg)

                elif not first_user_message and ev_type == "item_completed":
                    item = payload.get("item")
                    if isinstance(item, dict) and item.get("type") == "UserMessage":
                        extracted = _extract_user_text_from_content(item.get("content"))
                        if extracted and substantive(extracted):
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
                if extracted and substantive(extracted):
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



def rollout_path_for_uuid(directory: Path, thread_id: str, path: Path) -> Optional[Path]:
    """Accept a target rollout path inside the supplied session store."""
    resolved = path.resolve()
    filename = resolved.name.lower()
    suffix = f"{thread_id}.jsonl"
    if directory.resolve() in resolved.parents and (filename == suffix or filename.endswith(f"-{suffix}")):
        return resolved
    return None


def session_by_uuid(directory: Path, thread_id: str, hint: str = "") -> Optional[Dict[str, Any]]:
    """Read metadata only from rollouts named for the requested UUID."""
    thread_id = canonical_uuid(thread_id)
    if not directory.is_dir():
        return None

    candidates = list(directory.rglob(f"*{thread_id}.jsonl"))
    if hint:
        candidates.append(Path(hint))

    seen = set()
    matches = []
    for candidate in candidates:
        path = rollout_path_for_uuid(directory, thread_id, candidate)
        if path is None or path in seen:
            continue
        seen.add(path)
        record = parse_session_meta(path)
        if record and record["thread_id"] == thread_id:
            matches.append(record)
    return max(matches, key=lambda item: item["mtime"], default=None)


def require_project_session(record: Optional[Dict[str, Any]], root: Path) -> Dict[str, Any]:
    if not record:
        raise SessionOwnershipError("No rollout for this UUID was found in the configured session store.")
    if not same_project(record.get("cwd"), root):
        raise SessionOwnershipError("The rollout belongs to a different project.")
    return record


def load_binding(cache: Path, root: Path, home: Path) -> Dict[str, Any]:
    record = read_json(cache) or {}
    if record and not same_project(record.get("cwd"), root):
        raise SessionOwnershipError("The cached binding does not belong to this project.")
    if record.get("codex_home") and not same_project(record["codex_home"], home):
        raise SessionOwnershipError("The cached binding uses a different Codex home.")
    if record.get("thread_id"):
        record["thread_id"] = canonical_uuid(record["thread_id"])
    mode = record.get("mode") or MODE_VISIBLE_WINDOW
    if record and mode not in MANAGED_MODES | {MODE_VISIBLE_WINDOW}:
        raise ValueError(f"Unrecognized session cache mode: {mode!r}")
    return record


def load_pending(root: Path, home: Path, cache: Path) -> Dict[str, Any]:
    pending = read_json(project_file(root, "launch.pending.json")) or {}
    if not pending:
        return pending
    if not (same_project(pending.get("cwd"), root) and same_project(pending.get("codex_home"), home)
            and same_project(pending.get("cache_file"), cache)):
        raise SessionOwnershipError("A pending visible launch uses a different project, cache or Codex home.")
    marker, since, settings = pending.get("marker"), pending.get("since"), pending.get("settings")
    if (not isinstance(marker, str) or not marker.startswith(BOOTSTRAP_PREFIX) or not marker.endswith("]")
            or not isinstance(since, (int, float)) or not math.isfinite(since)
            or not isinstance(settings, dict)
            or set(settings) != {"model", "reasoning_effort", "sandbox", "approval"}):
        raise ValueError("Invalid pending launch record.")
    canonical_uuid(marker[len(BOOTSTRAP_PREFIX):-1])
    if pending.get("resume"):
        pending["resume"] = canonical_uuid(pending["resume"])
    return pending


def retired_threads(record: Dict[str, Any]) -> set:
    retired = {canonical_uuid(item) for item in record.get("retired_managed_threads", [])}
    if record.get("stopped") and record.get("thread_id"):
        retired.add(record["thread_id"])
    return retired


def save_binding(cache: Path, record: Dict[str, Any], previous: Dict[str, Any]) -> Dict[str, Any]:
    retired = retired_threads(previous)
    previous_id = previous.get("thread_id")
    thread_id = record.get("thread_id")
    if thread_id:
        record["thread_id"] = canonical_uuid(thread_id)
    if previous_id and previous_id != thread_id and previous.get("mode") in MANAGED_MODES:
        retired.add(previous_id)
    if thread_id and not record.get("stopped"):
        retired.discard(thread_id)
    if record.get("stopped") and thread_id:
        retired.add(thread_id)
    identity = {key: value for key, value in record.items() if key != "retired_managed_threads"}
    identity["updated_at"] = utc_now_iso()
    if retired:
        identity["retired_managed_threads"] = sorted(retired)
    atomic_json(cache, identity)
    return identity


def visible_binding(meta: Dict[str, Any], root: Path, home: Path, **settings: Any) -> Dict[str, Any]:
    record = {key: meta[key] for key in (
        "thread_id", "session_file", "session_timestamp", "originator", "source", "branch",
        "model", "reasoning_effort",
    ) if meta.get(key) not in (None, "")}
    return {**record, **settings, "cwd": str(root), "codex_home": str(home), "mode": MODE_VISIBLE_WINDOW}


def binding_state(record: Dict[str, Any], root: Path, home: Path, directory: Path,
                  deadline: Deadline) -> str:
    mode = record.get("mode") or MODE_VISIBLE_WINDOW
    thread_id = record.get("thread_id")
    if mode == MODE_DAEMON_MANAGED:
        if record.get("stopped") or not thread_id:
            return "DAEMON_SESSION_OFFLINE"
        cli = CodexCLI.resolve(home)
        return "CACHED" if check_daemon_thread_loaded(cli, deadline, thread_id, root) else "DAEMON_SESSION_OFFLINE"
    if mode == MODE_SANDBOX_BACKGROUND:
        if record.get("stopped") or not thread_id:
            return "SANDBOX_SESSION_OFFLINE"
        meta = session_by_uuid(directory, thread_id, record.get("session_file", ""))
        require_project_session(meta, root)
        return "CACHED" if legacy_process_state(record) == "alive" else "SANDBOX_SESSION_OFFLINE"
    if thread_id:
        meta = session_by_uuid(directory, thread_id, record.get("session_file", ""))
        if meta and same_project(meta.get("cwd"), root) and has_active_writer(home, thread_id):
            return "CACHED"
    return "HISTORICAL"


def candidate_result(status: str, matches: List[Dict[str, Any]], limit: int, field: str) -> Dict[str, Any]:
    entries = [serialize_candidate(enrich_session(match)) for match in matches[:limit]]
    return {"status": status, field: entries, "total": len(matches), "truncated": len(matches) > limit}


def serialize_candidate(record: Dict[str, Any]) -> Dict[str, Any]:
    value = {key: record.get(key, "") for key in (
        "thread_id", "model", "reasoning_effort", "last_active_local", "session_timestamp",
        "first_user_message", "branch", "originator", "source", "session_file",
    )}
    value["short_id"] = record["thread_id"][:8]
    return value


def _contains_bootstrap(path: Path, marker: str) -> bool:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                if marker not in line:
                    continue
                try:
                    candidate = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(candidate, dict) and first_line(user_message(candidate)).split(" ", 1)[0] == marker:
                    return True
    except FileNotFoundError:
        pass
    return False


def bootstrap_probe(directory: Path, root: Path, marker: str, since: float,
                    thread_id: str = "") -> Dict[str, Any]:
    if thread_id:
        meta = session_by_uuid(directory, thread_id)
        matches = [meta] if meta and same_project(meta.get("cwd"), root) else []
    else:
        matches = scan_matching_sessions(directory, normalize_path(root), since_ts=since)
    attributable = [meta for meta in matches if _contains_bootstrap(Path(meta["session_file"]), marker)]
    if len(attributable) == 1:
        return {"status": "BOOTSTRAP_READY", "session": attributable[0]}
    if len(matches) > 1:
        return candidate_result("MULTIPLE_MATCHES", matches, 5, "candidates")
    return {"status": "BOOTSTRAP_PENDING"}


def run_bootstrap_probe(directory: Path, root: Path, pending: Dict[str, Any],
                        deadline: Deadline) -> Dict[str, Any]:
    argv = [
        sys.executable, "-B", str(Path(__file__).resolve()), "--cwd", str(root),
        "--sessions-dir", str(directory), "--bootstrap-marker", pending["marker"],
        "--since", str(pending["since"]), "--json",
    ]
    if pending.get("resume"):
        argv.extend(["--bootstrap-thread", pending["resume"]])
    proc = subprocess.run(
        argv, capture_output=True, encoding="utf-8", errors="replace", timeout=deadline.require(),
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
    )
    result = json.loads(proc.stdout)
    if (proc.returncode, result.get("status")) not in (
        (0, "BOOTSTRAP_READY"), (2, "MULTIPLE_MATCHES"), (3, "BOOTSTRAP_PENDING"),
    ):
        raise OSError(result.get("note") or proc.stderr or "The visible session probe failed.")
    return result


def emit_output(data: Dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    print(f"STATUS: {data['status']}")
    for key, label in (
        ("thread_id", "THREAD_ID"), ("cwd", "CWD"), ("mode", "MODE"), ("model", "MODEL"),
        ("reasoning_effort", "EFFORT"), ("dispatch_state", "DISPATCH_STATE"), ("turn_id", "TURN_ID"),
        ("cache_file", "CACHE_FILE"), ("total", "TOTAL"), ("note", "NOTE"),
    ):
        if data.get(key) not in ("", None):
            print(f"{label}: {data[key]}")
    if data.get("truncated"):
        print("TRUNCATED: Use --limit to request more entries.")
    entries = data.get("history") or data.get("candidates") or []
    for index, item in enumerate(entries, 1):
        print(f"{index}. {item['thread_id']} | model={item['model'] or 'unknown'}"
              f" | effort={item['reasoning_effort'] or 'unknown'}"
              f" | active={item['last_active_local'] or 'unknown'}"
              f" | first={item['first_user_message'] or 'unknown'}")


def _discover(args: argparse.Namespace, root: Path, home: Path, directory: Path,
              cache: Path, deadline: Deadline, since: Optional[float]) -> Tuple[Dict[str, Any], int]:
    previous = load_binding(cache, root, home)
    pending = load_pending(root, home, cache)
    pending_path = project_file(root, "launch.pending.json")
    if pending and not args.select:
        result = run_bootstrap_probe(directory, root, pending, deadline)
        if result["status"] == "BOOTSTRAP_READY":
            meta = require_project_session(result["session"], root)
            previous = save_binding(cache, visible_binding(meta, root, home, **pending["settings"]), previous)
            pending_path.unlink(missing_ok=True)
        else:
            return (result, 2) if result["status"] == "MULTIPLE_MATCHES" else ({"status": "START_PENDING"}, 3)

    managed = previous.get("mode") in MANAGED_MODES
    retired = retired_threads(previous)
    if previous and (managed or not args.select):
        status = binding_state(previous, root, home, directory, deadline)
        if status == "CACHED" and (not args.select or args.select == previous.get("thread_id")):
            return {**previous, "status": "SELECTED" if args.select else status}, 0
        if managed and (not args.select or args.select == previous.get("thread_id")):
            return {**previous, "status": status}, 3
        if managed and status == "CACHED":
            raise SessionOwnershipError("Stop or explicitly replace the current managed binding before selecting another thread.")

    if args.select:
        meta = require_project_session(session_by_uuid(directory, args.select), root)
        if args.select in retired or not has_active_writer(home, args.select):
            return {"status": "HISTORICAL", "thread_id": args.select,
                    "note": "This thread is historical. Use the explicit resume flow."}, 3
        record = save_binding(cache, visible_binding(enrich_session(meta), root, home), previous)
        if pending:
            pending_path.unlink(missing_ok=True)
        return {**record, "status": "SELECTED"}, 0

    matches = scan_matching_sessions(directory, normalize_path(root), include_subagents=args.include_subagents,
                                     since_ts=since)
    if not matches:
        return {"status": "NOT_FOUND"}, 1
    recent = matches
    if since is None and args.recent_hours > 0:
        threshold = dt.datetime.now().timestamp() - args.recent_hours * 3600
        recent = [entry for entry in matches if entry["mtime"] >= threshold]
    live = [meta for meta in recent if meta["thread_id"] not in retired and has_active_writer(home, meta["thread_id"])]
    if not live:
        return candidate_result("HISTORICAL", matches, args.limit or 5, "candidates"), 3
    if len(live) > 1:
        return candidate_result("MULTIPLE_MATCHES", live, args.limit or 5, "candidates"), 2
    record = save_binding(cache, visible_binding(enrich_session(live[0]), root, home), previous)
    return {**record, "status": "AUTO_SELECTED"}, 0


def main() -> int:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(description="Resolve a verified Codex thread for a project.")
    parser.add_argument("--cwd", default=os.getcwd(), help="Target project root.")
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--refresh", action="store_true", help="Recheck the binding without dropping managed ownership.")
    operation.add_argument("--select", default="", help="Pin a live UUID belonging to this project.")
    operation.add_argument("--list-history", action="store_true", help="List saved project sessions without pinning.")
    operation.add_argument("--bootstrap-marker", default="", help=argparse.SUPPRESS)
    parser.add_argument("--bootstrap-thread", default="", help=argparse.SUPPRESS)
    parser.add_argument("--since", default="", help="Filter candidates by creation time (epoch or ISO 8601).")
    parser.add_argument("--codex-home", default="", help="Codex home for CLI, rollout and writer discovery.")
    parser.add_argument("--sessions-dir", default="", help="Override the rollout directory.")
    parser.add_argument("--cache-file", default="", help="Override the project session cache path.")
    parser.add_argument("--recent-hours", type=float, default=24.0, help="Recency threshold (0 disables it).")
    parser.add_argument("--limit", type=int, default=0, help="Return at most this many candidates (default 5, or 10 for history).")
    parser.add_argument("--timeout", type=float, default=15.0, help="Total seconds for binding and daemon probes.")
    parser.add_argument("--include-subagents", action="store_true", help="Include Codex internal subagent rollouts.")
    parser.add_argument("--json", action="store_true", help="Emit JSON.")
    args = parser.parse_args()

    try:
        deadline = Deadline(args.timeout)
        if args.limit < 0 or not math.isfinite(args.recent_hours) or args.recent_hours < 0:
            raise ValueError("--limit and --recent-hours must be non-negative and finite.")
        args.select = canonical_uuid(args.select) if args.select else ""
        since = parse_timestamp(args.since) if args.since else None
        if args.since and since is None:
            raise ValueError("--since requires a finite epoch or an ISO 8601 timestamp.")
        root = resolve_project(args.cwd)
        home = codex_home(args.codex_home)
        directory = local_path(args.sessions_dir, home / "sessions")
        if args.bootstrap_marker:
            if since is None or not args.bootstrap_marker.startswith(BOOTSTRAP_PREFIX):
                raise ValueError("A bootstrap probe requires a marker and --since.")
            result = bootstrap_probe(directory, root, args.bootstrap_marker, since,
                                     canonical_uuid(args.bootstrap_thread) if args.bootstrap_thread else "")
            emit_output(result, args.json)
            return {"BOOTSTRAP_READY": 0, "MULTIPLE_MATCHES": 2, "BOOTSTRAP_PENDING": 3}[result["status"]]
        if args.list_history:
            matches = scan_matching_sessions(directory, normalize_path(root), include_subagents=args.include_subagents,
                                             since_ts=since)
            data = candidate_result("HISTORY_LISTED", matches, args.limit or 10, "history") if matches else {"status": "NOT_FOUND"}
            code = 0 if matches else 1
        else:
            cache = project_file(root, "session.json", args.cache_file)
            # Read-only paths (history and the internal bootstrap probe) never take this lock.
            with project_lock(root, "session.lock", deadline):
                data, code = _discover(args, root, home, directory, cache, deadline, since)
            if code == 0:
                data.setdefault("cache_file", str(cache))
        emit_output({"cwd": str(root), **data}, args.json)
        return code
    except (SessionOwnershipError, ThreadOwnershipError) as exc:
        emit_output({"status": "INVALID_SESSION", "note": str(exc)}, args.json)
        return 2
    except (DaemonRPCError, OSError, RuntimeError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        emit_output({"status": "SESSION_PROBE_FAILED", "note": str(exc)}, args.json)
        return 1
    except ValueError as exc:
        emit_output({"status": "INVALID_ARGUMENTS", "note": str(exc)}, args.json)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
