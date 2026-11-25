#!/usr/bin/env python3
"""Launch, resume, or stop a project-scoped Codex session."""

from __future__ import annotations

import argparse
import base64
import ctypes
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple

try:
    import tomllib
except ImportError:  # Python 3.8?3.10
    tomllib = None

from codex_daemon_rpc import (
    CodexDaemonClient, DaemonProbeError, DaemonRPCError, DaemonUnavailableError,
    ThreadOwnershipError, ensure_daemon_running, get_daemon_status,
)
from codex_session import (
    BOOTSTRAP_PREFIX, MODE_DAEMON_MANAGED, MODE_SANDBOX_BACKGROUND, MODE_VISIBLE_WINDOW,
    CodexCLI, Deadline, atomic_json, canonical_uuid, codex_home, configure_utf8_stdio, has_active_writer,
    local_path, project_file, project_lock, resolve_project, same_project,
)
from codex_windows import legacy_process_state
from find_codex_thread import (
    SessionOwnershipError, binding_state, load_binding, load_pending, require_project_session,
    run_bootstrap_probe, save_binding, session_by_uuid, visible_binding,
)

SANDBOX_CHOICES = ("read-only", "workspace-write", "danger-full-access")
APPROVAL_CHOICES = ("on-request", "never")
KNOWN_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
_UOI_NAME = 2


class DesktopError(RuntimeError):
    pass


@dataclass
class SessionContext:
    root: Path
    home: Path
    directory: Path
    cache: Path
    deadline: Deadline


def detect_windows_desktop() -> str:
    """Return the current Windows thread desktop name."""
    if os.name != "nt":
        return ""
    try:
        import ctypes.wintypes as wt

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentThreadId.restype = wt.DWORD
        user32.GetThreadDesktop.argtypes = [wt.DWORD]
        user32.GetThreadDesktop.restype = wt.HANDLE
        user32.GetUserObjectInformationW.argtypes = [
            wt.HANDLE,
            ctypes.c_int,
            wt.LPVOID,
            wt.DWORD,
            ctypes.POINTER(wt.DWORD),
        ]
        user32.GetUserObjectInformationW.restype = wt.BOOL

        hdesk = user32.GetThreadDesktop(kernel32.GetCurrentThreadId())
        if not hdesk:
            return ""
        needed = wt.DWORD(0)
        user32.GetUserObjectInformationW(hdesk, _UOI_NAME, None, 0, ctypes.byref(needed))
        if not needed.value:
            return ""
        buf = ctypes.create_unicode_buffer(needed.value // 2 + 1)
        ok = user32.GetUserObjectInformationW(hdesk, _UOI_NAME, buf, needed.value, ctypes.byref(needed))
        return buf.value.strip() if ok else ""
    except (AttributeError, OSError, ValueError):
        return ""


def parse_top_level_toml(config_path: Path) -> Dict[str, Any]:
    """Read top-level scalar settings from ~/.codex/config.toml (Python 3.8+ zero-dep)."""
    try:
        raw_text = config_path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return {}

    keys = ("model", "model_reasoning_effort", "sandbox_mode", "approval_policy", "service_tier")
    if tomllib is not None:
        try:
            parsed = tomllib.loads(raw_text)
            if isinstance(parsed, dict):
                return {k: parsed[k] for k in keys if k in parsed and not isinstance(parsed[k], (dict, list))}
        except Exception:
            pass

    result: Dict[str, Any] = {}
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            break
        match = re.match(r"^([A-Za-z0-9_.-]+)\s*=\s*(.+?)\s*$", line)
        if not match:
            continue
        key, val = match.group(1), match.group(2)
        if key not in keys:
            continue
        if val.startswith('"'):
            m_str = re.match(r'^"((?:[^"\\]|\\.)*)"', val)
            if m_str:
                result[key] = m_str.group(1)
        elif val.startswith("'"):
            m_str = re.match(r"^'([^']*)'", val)
            if m_str:
                result[key] = m_str.group(1)
        else:
            cleaned = val.split("#", 1)[0].strip()
            if cleaned:
                result[key] = cleaned
    return result


def load_models_catalog(
    codex_home: Path,
    include_hidden: bool = False,
) -> Dict[str, Any]:
    """Read ~/.codex/models_cache.json and ~/.codex/config.toml for real account models."""
    cache_path = codex_home / "models_cache.json"
    config_path = codex_home / "config.toml"
    config_defaults = parse_top_level_toml(config_path)

    models_all: List[Dict[str, Any]] = []
    fetched_at = ""
    client_version = ""
    cache_exists = False
    cache_error = ""
    try:
        cache_exists = cache_path.is_file()
    except OSError as exc:
        cache_error = str(exc)

    if cache_exists:
        try:
            raw = json.loads(cache_path.read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict):
                fetched_at = str(raw.get("fetched_at") or "")
                client_version = str(raw.get("client_version") or "")
                raw_models = raw.get("models")
                if isinstance(raw_models, list):
                    for item in raw_models:
                        if not isinstance(item, dict):
                            continue
                        slug = str(item.get("slug") or "").strip()
                        if not slug:
                            continue
                        visibility = str(item.get("visibility") or "list").strip().lower()
                        raw_levels = item.get("supported_reasoning_levels")
                        efforts: List[str] = []
                        if isinstance(raw_levels, list):
                            for lvl in raw_levels:
                                if isinstance(lvl, dict) and lvl.get("effort"):
                                    efforts.append(str(lvl["effort"]).strip().lower())
                                elif isinstance(lvl, str) and lvl.strip():
                                    efforts.append(lvl.strip().lower())
                        priority_raw = item.get("priority")
                        priority = priority_raw if isinstance(priority_raw, int) else 9999
                        models_all.append(
                            {
                                "slug": slug,
                                "display_name": str(item.get("display_name") or slug),
                                "description": str(item.get("description") or "").strip(),
                                "default_reasoning_level": str(item.get("default_reasoning_level") or "").strip().lower(),
                                "supported_efforts": efforts,
                                "visibility": visibility,
                                "priority": priority,
                            }
                        )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            cache_error = str(exc)

    models_all.sort(key=lambda m: (m["priority"], m["slug"]))
    visible_models = (
        list(models_all)
        if include_hidden
        else [m for m in models_all if m["visibility"] != "hide"]
    )

    return {
        "codex_home": str(codex_home),
        "models_cache_file": str(cache_path),
        "models_cache_exists": cache_exists,
        "models_cache_error": cache_error,
        "fetched_at": fetched_at,
        "client_version": client_version,
        "config_file": str(config_path),
        "config_exists": config_path.is_file(),
        "config_defaults": config_defaults,
        "models": visible_models,
        "all_models_by_slug": {m["slug"]: m for m in models_all},
    }



def launch_mode(platform: str, desktop: str) -> str:
    if not platform.startswith("win"):
        return MODE_VISIBLE_WINDOW
    name = desktop.lower()
    if name == "default":
        return MODE_VISIBLE_WINDOW
    if re.fullmatch(r"exebox-[a-z0-9._-]+|codexsandboxdesktop-[0-9a-f]{32}", name):
        return MODE_DAEMON_MANAGED
    if not name:
        raise DesktopError("Windows desktop detection failed. Launch mode could not be determined.")
    raise DesktopError(f"Unsupported Windows desktop: {desktop!r}.")


def effective_platform() -> str:
    return "win32" if os.name == "nt" else sys.platform


def emit_result(data: Dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    print(f"STATUS: {data['status']}")
    for key, label in (
        ("thread_id", "THREAD_ID"), ("turn_id", "TURN_ID"), ("cwd", "CWD"),
        ("desktop", "DESKTOP"), ("mode", "MODE"), ("launch_mode", "LAUNCH_MODE"),
        ("model", "MODEL"), ("reasoning_effort", "EFFORT"), ("sandbox", "SANDBOX"),
        ("approval", "APPROVAL"), ("turn_status", "TURN_STATUS"),
        ("unsubscribe_status", "UNSUBSCRIBE_STATUS"), ("dispatch_state", "DISPATCH_STATE"),
        ("session_file", "SESSION_FILE"), ("cache_file", "CACHE_FILE"),
        ("cleanup_status", "CLEANUP_STATUS"), ("launcher", "LAUNCHER"), ("cli_path", "CLI_PATH"),
        ("launch_argv", "LAUNCH_ARGV"), ("models_cache_file", "MODELS_CACHE"),
        ("fetched_at", "FETCHED_AT"), ("total", "TOTAL"), ("daemon_status", "DAEMON_STATUS"),
        ("note", "NOTE"),
    ):
        if data.get(key) not in (None, ""):
            print(f"{label}: {data[key]}")
    if data.get("truncated"):
        print("TRUNCATED: Use --limit to request more entries.")
    for index, model in enumerate(data.get("models", []), 1):
        efforts = ", ".join(model["supported_efforts"]) or "unknown"
        print(f"{index}. {model['slug']} ({model['display_name']}) | efforts={efforts}")
    for index, candidate in enumerate(data.get("candidates", []), 1):
        print(f"{index}. {candidate['thread_id']} | model={candidate['model'] or 'unknown'}"
              f" | first={candidate['first_user_message'] or 'unknown'}")


def list_models(args: argparse.Namespace, home: Path, deadline: Deadline) -> Tuple[Dict[str, Any], int]:
    catalog = load_models_catalog(home, args.include_hidden)
    models = catalog["models"]
    desktop = detect_windows_desktop()
    mode = launch_mode(effective_platform(), desktop)
    daemon_status = ""
    if mode == MODE_DAEMON_MANAGED:
        try:
            daemon_status = get_daemon_status(CodexCLI.resolve(home), deadline)["status"]
        except (DaemonRPCError, OSError) as exc:
            daemon_status = f"unknown ({exc})"
    return {
        "status": "MODELS_LISTED" if models else "NO_MODELS_CACHE",
        "desktop": desktop, "launch_mode": mode, "daemon_status": daemon_status,
        "codex_home": str(home), "client_version": catalog["client_version"],
        "models_cache_file": catalog["models_cache_file"], "fetched_at": catalog["fetched_at"],
        "config_file": catalog["config_file"], "config_defaults": catalog["config_defaults"],
        "models": models[:args.limit] if args.limit else models,
        "total": len(models), "truncated": bool(args.limit and len(models) > args.limit),
        "note": catalog["models_cache_error"] or "Source: local models_cache.json.",
    }, 0 if models else 1


def _powershell_payload(cli: CodexCLI, cwd: Path, codex_args: List[str]) -> str:
    application, parameters, environment = cli.windows_command(codex_args)
    options = {
        "cwd": str(cwd), "application": application, "parameters": parameters, "environment": environment,
    }
    payload = base64.b64encode(json.dumps(options, ensure_ascii=True).encode("ascii")).decode("ascii")
    # Paths and arguments are decoded as data. No user value is parsed as PowerShell source.
    command = (
        '$ErrorActionPreference = "Stop"\n'
        f"$p = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}')) | ConvertFrom-Json\n"
        'foreach ($v in $p.environment.PSObject.Properties) { [Environment]::SetEnvironmentVariable($v.Name, [string]$v.Value) }\n'
        'Set-Location -LiteralPath $p.cwd\n'
        'Start-Process -FilePath $p.application -ArgumentList ([string]$p.parameters) '
        '-WorkingDirectory $p.cwd -NoNewWindow -Wait\n'
    )
    return base64.b64encode(command.encode("utf-16-le")).decode("ascii")


def build_visible_launch(cli: CodexCLI, root: Path, args: argparse.Namespace, message: str,
                         platform: str) -> Tuple[str, List[str]]:
    codex_args = ["resume"] if args.resume else []
    codex_args += ["-m", args.model, "-c", f"model_reasoning_effort={args.effort}"]
    codex_args.extend(["-C", str(root)])
    if args.sandbox:
        codex_args.extend(["-s", args.sandbox])
    if args.approval:
        codex_args.extend(["-a", args.approval])
    if args.resume:
        codex_args.append(args.resume)
    codex_args.append(message)

    if platform.startswith("win"):
        powershell = shutil.which("powershell.exe")
        if not powershell:
            raise FileNotFoundError("Windows PowerShell was not found.")
        argv = [powershell, "-NoProfile", "-NoExit", "-EncodedCommand", _powershell_payload(cli, root, codex_args)]
        wt = shutil.which("wt.exe")
        return ("wt.exe", [wt, "-w", "new", "new-tab", *argv]) if wt else ("powershell", argv)

    shell = " ".join(shlex.quote(arg) for arg in [
        "env", f"CODEX_HOME={cli.home}", str(cli.path), *codex_args,
    ])
    shell_line = f"cd {shlex.quote(str(root))} && {shell}"
    if platform == "darwin":
        script = shell_line.replace("\\", "\\\\").replace('"', '\\"')
        return "osascript-terminal", [
            "osascript", "-e", f'tell application "Terminal" to do script "{script}"',
            "-e", 'tell application "Terminal" to activate',
        ]

    cwd = str(root)
    shell_line += '; exec "${SHELL:-bash}"'
    shell_args = ["bash", "-lc", shell_line]
    terminals = [
        ("x-terminal-emulator", ["-e", *shell_args]),
        ("gnome-terminal", ["--working-directory", cwd, "--", *shell_args]),
        ("konsole", ["--workdir", cwd, "-e", *shell_args]),
        ("xfce4-terminal", ["--working-directory", cwd, "-e", "bash -lc " + shlex.quote(shell_line)]),
        ("kitty", ["--directory", cwd, *shell_args]),
        ("alacritty", ["--working-directory", cwd, "-e", *shell_args]),
        ("wezterm", ["start", "--cwd", cwd, "--", *shell_args]),
        ("xterm", ["-e", *shell_args]),
    ]
    for name, options in terminals:
        terminal = shutil.which(name)
        if terminal:
            return name, [terminal, *options]
    raise FileNotFoundError("No supported visible terminal emulator was found.")


def launch_terminal_window(argv: List[str], cwd: Path) -> Any:
    if os.name == "nt":
        shell_exec = ctypes.windll.shell32.ShellExecuteW
        shell_exec.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                               ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int]
        shell_exec.restype = ctypes.c_void_p
        result = shell_exec(None, "open", argv[0], subprocess.list2cmdline(argv[1:]), str(cwd), 1)
        if result and result > 32:
            return None  # The shell accepted the request. The rollout is the readiness proof.
        return subprocess.Popen(argv, cwd=cwd, close_fds=True,
                                creationflags=subprocess.CREATE_NEW_CONSOLE)
    return subprocess.Popen(argv, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)


def _session_rollout(context: SessionContext, thread_id: str, previous: Dict[str, Any]) -> Any:
    hint = previous.get("session_file", "") if previous.get("thread_id") == thread_id else ""
    return session_by_uuid(context.directory, thread_id, hint)


def stop_cached_session(context: SessionContext, record: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
    if not record:
        return {"status": "NO_ACTIVE_SESSION"}, 0
    mode = record.get("mode") or MODE_VISIBLE_WINDOW
    thread_id = record.get("thread_id")
    identity = {"thread_id": thread_id, "mode": mode}
    if mode == MODE_VISIBLE_WINDOW:
        return {**identity, "status": "SKIPPED_VISIBLE_WINDOW",
                "note": "Close the visible Codex window from its desktop."}, 0
    try:
        if record.get("stopped"):
            return {**identity, "status": "ALREADY_EXITED"}, 0
        if not thread_id:
            raise SessionOwnershipError("The legacy record has no UUID. Its process cannot be safely attributed.")
        thread_id = canonical_uuid(thread_id)
        rollout = _session_rollout(context, thread_id, record)
        if rollout:
            require_project_session(rollout, context.root)
        if mode == MODE_SANDBOX_BACKGROUND:
            require_project_session(rollout, context.root)
            state = legacy_process_state(record, terminate=True, timeout=context.deadline.require())
            if state == "fingerprint_mismatch":
                raise SessionOwnershipError("The legacy process fingerprint does not match.")
            while state == "terminated" and has_active_writer(context.home, thread_id):
                time.sleep(min(0.1, context.deadline.require()))
            save_binding(context.cache, {**record, "stopped": True}, record)
            return {**identity, "status": "STOPPED" if state == "terminated" else "ALREADY_EXITED"}, 0

        cli = CodexCLI.resolve(context.home)
        status = get_daemon_status(cli, context.deadline)
        turn_status = "notLoaded"
        unsubscribe_status = "notLoaded"
        if status["running"]:
            with CodexDaemonClient(cli, context.deadline, status["socket_path"]) as client:
                if thread_id in client.list_loaded_threads():
                    client.verify_thread(thread_id, context.root)
                    turn_status = client.interrupt_active_turn(thread_id)
                    unsubscribe_status = client.unsubscribe_thread(thread_id)
                    if unsubscribe_status != "notLoaded":
                        after = client.read_thread(thread_id)
                        runtime = after.get("status")
                        if (not same_project(after.get("cwd"), context.root) or not isinstance(runtime, dict)
                                or runtime.get("type") not in ("idle", "notLoaded")):
                            raise DaemonRPCError("A turn is active or the thread could not be verified after unsubscribe.")
        if turn_status != "notLoaded" or rollout:
            save_binding(context.cache, {**record, "stopped": True}, record)
        return {
            **identity, "status": "STOPPED" if turn_status != "notLoaded" else "ALREADY_EXITED",
            "turn_status": turn_status, "unsubscribe_status": unsubscribe_status,
            "note": "Turn state verified. The unsubscribe result applies to this proxy connection.",
        }, 0
    except (DaemonRPCError, OSError, ValueError, RuntimeError) as exc:
        return {**identity, "status": "STOP_FAILED", "note": str(exc)}, 1


def _verify_resume(context: SessionContext, cli: CodexCLI, thread_id: str, mode: str,
                   cached: Dict[str, Any], dry_run: bool) -> None:
    rollout = _session_rollout(context, thread_id, cached)
    if rollout or mode != MODE_DAEMON_MANAGED:
        require_project_session(rollout, context.root)
        return
    if dry_run:
        return  # The plan starts no processes. Apply-time ownership is still mandatory.
    status = get_daemon_status(cli, context.deadline)
    if not status["running"]:
        raise SessionOwnershipError("This UUID has no rollout and its daemon is offline.")
    with CodexDaemonClient(cli, context.deadline, status["socket_path"]) as client:
        if thread_id not in client.list_loaded_threads():
            raise SessionOwnershipError("This UUID has no rollout and is not loaded.")
        client.verify_thread(thread_id, context.root)


def _effective_settings(response: Dict[str, Any]) -> Dict[str, Any]:
    sandbox = response.get("sandbox")
    if isinstance(sandbox, dict):
        sandbox = sandbox.get("type")
    sandbox = {"readOnly": "read-only", "workspaceWrite": "workspace-write", "dangerFullAccess": "danger-full-access"}.get(
        sandbox, sandbox
    ) if isinstance(sandbox, str) else None
    return {
        "model": response.get("model"), "reasoning_effort": response.get("reasoningEffort"),
        "sandbox": sandbox, "approval": response.get("approvalPolicy"),
    }


def launch_daemon(context: SessionContext, cli: CodexCLI, args: argparse.Namespace,
                  previous: Dict[str, Any], settings: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
    status = ensure_daemon_running(cli, context.deadline)
    with CodexDaemonClient(cli, context.deadline, status["socket_path"]) as client:
        if args.resume and args.resume in client.list_loaded_threads():
            thread = client.read_thread(args.resume)
            if not same_project(thread.get("cwd"), context.root):
                raise ThreadOwnershipError("The thread to resume belongs to a different project.")
            runtime = thread.get("status")
            if not isinstance(runtime, dict) or runtime.get("type") not in ("idle", "notLoaded"):
                return {"status": "BINDING_CONFLICT", "thread_id": args.resume,
                        "note": "The thread to resume has work in progress or an unknown runtime state."}, 1

        response = client.start_thread(
            str(context.root), args.model, args.effort, args.sandbox, settings["approval"], args.resume,
        )
        thread = response.get("thread")
        if not isinstance(thread, dict):
            raise DaemonRPCError("The daemon returned no thread metadata.")
        try:
            thread_id = canonical_uuid(thread.get("id"))
        except ValueError as exc:
            raise DaemonRPCError(str(exc)) from exc
        if args.resume and thread_id != args.resume:
            raise ThreadOwnershipError("thread/resume returned a different UUID.")
        effective_root = thread.get("cwd") or response.get("cwd")
        if not effective_root:
            client.verify_thread(thread_id, context.root)
        elif not same_project(effective_root, context.root):
            raise ThreadOwnershipError("The daemon returned a thread in a different project.")

        effective = _effective_settings(response)
        if effective != settings:
            return {
                "status": "CONFIG_MISMATCH", "thread_id": thread_id, **effective,
                "requested": settings, "effective": effective,
                "note": "Effective model, effort, sandbox or approval differs from the request.",
            }, 1

        record = save_binding(context.cache, {
            "thread_id": thread_id, "cwd": str(context.root), "codex_home": str(context.home),
            "mode": MODE_DAEMON_MANAGED, "session_file": thread.get("path") or "",
            "dispatch_state": "pending", **effective,
        }, previous)
        try:
            turn = client.start_turn(thread_id, args.dispatch_message)
        except DaemonRPCError as exc:
            return {**record, "status": "DISPATCH_UNKNOWN", "dispatch_state": "unknown",
                    "note": f"Execution could not be confirmed. Inspect before retrying. {exc}"}, 1

        data = {**record, "turn_id": turn["id"], "dispatch_state": "started"}
        try:
            data = save_binding(context.cache, data, record)
        except OSError as exc:
            return {**data, "status": "DISPATCH_UNKNOWN",
                    "note": f"The turn started, but its ID could not be written to the cache. {exc}"}, 1
    return {**data, "status": "RESUMED_AND_BOUND" if args.resume else "STARTED_AND_BOUND",
            "turn_status": turn["status"]}, 0


def launch_visible(context: SessionContext, args: argparse.Namespace, previous: Dict[str, Any],
                   settings: Dict[str, Any], marker: str, argv: List[str]) -> Tuple[Dict[str, Any], int]:
    pending_path = project_file(context.root, "launch.pending.json")
    pending = {
        "marker": marker, "since": time.time(), "resume": args.resume, "settings": settings,
        "cwd": str(context.root), "codex_home": str(context.home), "cache_file": str(context.cache),
    }
    atomic_json(pending_path, pending)
    try:
        context.deadline.require()
        process = launch_terminal_window(argv, context.root)
    except OSError as exc:
        pending_path.unlink(missing_ok=True)
        return {"status": "LAUNCH_FAILED", "note": str(exc)}, 1

    last_multiple = None
    while context.deadline.remaining():
        exit_code = process.poll() if process is not None else None
        if exit_code not in (None, 0):
            pending_path.unlink(missing_ok=True)
            return {"status": "LAUNCH_FAILED", "note": f"Terminal launcher exited with code {exit_code}."}, 1
        try:
            result = run_bootstrap_probe(context.directory, context.root, pending, context.deadline)
        except subprocess.TimeoutExpired:
            break
        if result["status"] == "BOOTSTRAP_READY":
            meta = require_project_session(result["session"], context.root)
            data = visible_binding(meta, context.root, context.home, **settings)
            data = save_binding(context.cache, data, previous)
            pending_path.unlink(missing_ok=True)
            return {**data, "status": "RESUMED_AND_BOUND" if args.resume else "STARTED_AND_BOUND"}, 0
        if result["status"] == "MULTIPLE_MATCHES":
            last_multiple = result
        time.sleep(min(args.interval, context.deadline.remaining()))
    if last_multiple is not None:
        return last_multiple, 2
    return {
        "status": "RESUME_TIMEOUT" if args.resume else "START_TIMEOUT",
        "note": "No matching readiness message yet. Check the window and use find_codex_thread.py --refresh.",
    }, 124


def run_launch(args: argparse.Namespace, context: SessionContext) -> Tuple[Dict[str, Any], int]:
    platform = effective_platform()
    desktop = detect_windows_desktop()
    mode = launch_mode(platform, desktop)
    resolution = {"cwd": str(context.root), "desktop": desktop, "mode": mode, "cache_file": str(context.cache)}
    cli = CodexCLI.resolve(context.home)
    resolution["cli_path"] = str(cli.path)
    approval = args.approval or ("never" if mode == MODE_DAEMON_MANAGED else "")
    settings = {
        "model": args.model, "reasoning_effort": args.effort, "sandbox": args.sandbox,
        "approval": approval,
    }
    if mode == MODE_DAEMON_MANAGED and (not args.sandbox or (not args.dispatch_message and not args.dry_run)):
        raise ValueError("DAEMON_MANAGED requires --sandbox and, for a real launch, --dispatch-message.")
    if mode == MODE_VISIBLE_WINDOW and args.dispatch_message:
        raise ValueError("--dispatch-message is available only in DAEMON_MANAGED mode.")

    marker = f"{BOOTSTRAP_PREFIX}{uuid.uuid4()}]"
    message = marker + " " + ("Confirm the resumed window is ready without using tools." if args.resume else args.init_prompt)
    launcher = "app-server-daemon"
    argv = []
    if mode == MODE_VISIBLE_WINDOW:
        launcher, argv = build_visible_launch(cli, context.root, args, message, platform)
    resolution["launcher"] = launcher
    if args.dry_run:
        if args.resume:
            _verify_resume(context, cli, args.resume, mode, {}, dry_run=True)
        return {**resolution, **settings, "status": "DRY_RUN", "launch_argv": argv,
                "thread_id": args.resume, "dispatch_state": "planned" if args.dispatch_message else ""}, 0

    with project_lock(context.root, "session.lock", context.deadline):
        previous = load_binding(context.cache, context.root, context.home)
        pending = load_pending(context.root, context.home, context.cache)
        if pending and not args.force_new:
            return {**resolution, "status": "START_PENDING", "note": "Run find_codex_thread.py --refresh to check readiness."}, 3
        if args.resume:
            _verify_resume(context, cli, args.resume, mode, previous, dry_run=False)
        cleanup_status = ""
        if previous and args.force_new and previous.get("thread_id") != args.resume:
            cleanup, code = stop_cached_session(context, previous)
            cleanup_status = cleanup["status"]
            if code:
                return {**resolution, **cleanup}, code
            previous = load_binding(context.cache, context.root, context.home)
        elif previous and (not args.resume or previous.get("thread_id") != args.resume):
            try:
                cached_status = binding_state(previous, context.root, context.home, context.directory, context.deadline)
            except (OSError, DaemonRPCError, RuntimeError) as exc:
                return {**resolution, "status": "SESSION_PROBE_FAILED", "note": str(exc)}, 1
            if cached_status == "CACHED":
                return {
                    **resolution, **previous, "status": "BINDING_CONFLICT" if args.resume else "ALREADY_BOUND",
                    "note": "The cached live binding was retained. This invocation did not dispatch a message.",
                }, 1 if args.resume else 0

        if mode == MODE_DAEMON_MANAGED:
            data, code = launch_daemon(context, cli, args, previous, settings)
            if pending and data.get("dispatch_state") in ("started", "unknown"):
                project_file(context.root, "launch.pending.json").unlink(missing_ok=True)
        else:
            data, code = launch_visible(context, args, previous, settings, marker, argv)
        return {**resolution, "cleanup_status": cleanup_status, **data}, code


def main() -> int:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(description="Manage a Codex session for one project.")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--list-models", action="store_true", help="List saved models from models_cache.json.")
    action.add_argument("--stop", action="store_true", help="Stop the verified project-managed session.")
    action.add_argument("--resume", default="", help="Resume a project-owned UUID.")
    parser.add_argument("--cwd", default=os.getcwd(), help="Target project directory.")
    parser.add_argument("--model", default="", help="Exact model slug. Required to start or resume.")
    parser.add_argument("--effort", default="", help="Explicit reasoning effort. Required to start or resume.")
    parser.add_argument("--sandbox", choices=SANDBOX_CHOICES, default="", help="Codex sandbox policy.")
    parser.add_argument("--approval", choices=APPROVAL_CHOICES, default="", help="Daemon default: never.")
    parser.add_argument("--force-new", action="store_true", help="Explicitly replace a previous binding.")
    parser.add_argument("--timeout", type=float, default=25.0, help="Total seconds for startup and probes.")
    parser.add_argument("--interval", type=float, default=1.0, help="Visible rollout poll interval.")
    parser.add_argument("--init-prompt", default="hi", help="Initial prompt for a new visible window.")
    parser.add_argument("--dry-run", action="store_true", help="Print a launch plan without writing or starting a process.")
    parser.add_argument("--dispatch-message", default="", help="Start this message on the new daemon connection before returning.")
    parser.add_argument("--codex-home", default="", help="Codex home used by every child CLI and rollout lookup.")
    parser.add_argument("--sessions-dir", default="", help="Override the rollout directory.")
    parser.add_argument("--cache-file", default="", help="Override the project session cache file.")
    parser.add_argument("--include-hidden", action="store_true", help="Allow hidden models in catalog output and selection.")
    parser.add_argument("--limit", type=int, default=0, help="Limit model output (0 lists all).")
    parser.add_argument("--json", action="store_true", help="Emit JSON.")
    args = parser.parse_args()

    try:
        deadline = Deadline(args.timeout)
        if not math.isfinite(args.interval) or args.interval <= 0 or args.limit < 0:
            raise ValueError("--interval must be finite and positive and --limit must be non-negative.")
        if (args.stop or args.list_models) and (
            args.dry_run or args.force_new or args.dispatch_message or args.model or args.effort
            or args.sandbox or args.approval or args.init_prompt != "hi"
        ):
            raise ValueError("--stop and --list-models cannot be combined with launch options.")
        if args.limit and not args.list_models:
            raise ValueError("--limit requires --list-models.")

        home = codex_home(args.codex_home)
        if args.list_models:
            data, code = list_models(args, home, deadline)
        else:
            root = resolve_project(args.cwd)
            cache = project_file(root, "session.json", args.cache_file)
            context = SessionContext(root, home, local_path(args.sessions_dir, home / "sessions"), cache, deadline)
            if args.stop:
                with project_lock(root, "session.lock", deadline):
                    data, code = stop_cached_session(context, load_binding(cache, root, home))
                data.update(cwd=str(root), cache_file=str(cache))
            else:
                args.resume = canonical_uuid(args.resume) if args.resume else ""
                args.model, args.effort = args.model.strip(), args.effort.strip().lower()
                if not args.model or not args.effort:
                    raise ValueError("--model and --effort must be explicitly specified.")
                catalog = load_models_catalog(home, args.include_hidden)
                model = next((entry for entry in catalog["models"] if entry["slug"] == args.model), None)
                if catalog["all_models_by_slug"] and not model:
                    raise ValueError(f"Model {args.model!r} is outside the selected model catalog.")
                efforts = model["supported_efforts"] if model and model["supported_efforts"] else KNOWN_EFFORTS
                if args.effort not in efforts:
                    raise ValueError(f"Unsupported reasoning effort for {args.model!r}: choose from {', '.join(efforts)}.")
                if args.dispatch_message and not args.dispatch_message.strip():
                    raise ValueError("--dispatch-message must contain text.")
                data, code = run_launch(args, context)
    except (SessionOwnershipError, ThreadOwnershipError) as exc:
        data, code = {"status": "INVALID_SESSION", "note": str(exc)}, 2
    except DesktopError as exc:
        data, code = {"status": "DESKTOP_UNAVAILABLE", "note": str(exc)}, 1
    except DaemonUnavailableError as exc:
        data, code = {"status": "DAEMON_UNAVAILABLE", "note": str(exc)}, 1
    except DaemonProbeError as exc:
        data, code = {"status": "SESSION_PROBE_FAILED", "note": str(exc)}, 1
    except DaemonRPCError as exc:
        data, code = {"status": "DAEMON_RPC_ERROR", "note": str(exc)}, 1
    except (OSError, RuntimeError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        data, code = {"status": "OPERATION_FAILED", "note": str(exc)}, 1
    except ValueError as exc:
        data, code = {"status": "INVALID_ARGUMENTS", "note": str(exc)}, 2

    emit_result(data, args.json)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
