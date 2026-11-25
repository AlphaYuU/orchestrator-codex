#!/usr/bin/env python3
"""Launch, resume, or stop a Codex CLI Subagent window/session with explicit user-chosen
model, reasoning effort, and permissions.

- Windows main desktop ('Default') / macOS / Linux: opens a visible standalone terminal
  window (`VISIBLE_WINDOW` via ShellExecuteW + wt.exe on Windows).
- Windows isolated sandbox desktop (e.g. 'exebox-*'): runs natively inside Codex's
  persistent local app-server daemon (`DAEMON_MANAGED` via `codex app-server proxy`
  JSON-RPC `thread/start`, `thread/resume`, `thread/loaded/list`, `thread/unsubscribe`),
  spawning ZERO `wt.exe` / `conhost.exe` windows and surviving `run_command` exits.
- Supports --list-models, --resume <UUID>, --stop, --force-new, and --dry-run.

Zero third-party dependencies. Compatible with Python 3.8+ on Windows 11, macOS, and Linux.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover - Python 3.8-3.10 fallback
    tomllib = None  # type: ignore[assignment]

SANDBOX_CHOICES = ("read-only", "workspace-write", "danger-full-access")
APPROVAL_CHOICES = ("untrusted", "on-failure", "on-request", "never")
STANDARD_EFFORTS = ("low", "medium", "high", "xhigh")
KNOWN_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
ALLOWED_PID_NAMES_WIN = frozenset(("conhost.exe", "powershell.exe", "codex.exe"))
ALLOWED_PID_NAMES_POSIX = frozenset(("codex", "bash", "zsh", "sh"))

MODE_VISIBLE_WINDOW = "VISIBLE_WINDOW"
MODE_DAEMON_MANAGED = "DAEMON_MANAGED"
MODE_SANDBOX_BACKGROUND = "SANDBOX_BACKGROUND"

_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from codex_daemon_rpc import (  # noqa: E402
    CodexDaemonClient,
    DaemonRPCError,
    DaemonUnavailableError,
    check_daemon_thread_loaded,
    ensure_daemon_running,
    get_daemon_status,
)

_FILETIME_UNIX_EPOCH_TICKS = 11644473600 * 10_000_000
_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_TH32CS_SNAPPROCESS = 0x00000002
_UOI_NAME = 2


def configure_utf8_stdio() -> None:
    """Ensure UTF-8 console output on Windows (avoids GBK crashes on Unicode paths)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except OSError:
                pass


def safe_is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def safe_is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def resolve_codex_home(override: str = "") -> Path:
    if override:
        return Path(override).expanduser()
    env_home = os.environ.get("CODEX_HOME")
    if env_home:
        return Path(env_home).expanduser()
    return Path.home() / ".codex"


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


# ---------------------------------------------------------------------------
# Windows desktop detection & anti-mis-kill Win32 PID fingerprint utilities
# ---------------------------------------------------------------------------

def detect_windows_desktop() -> str:
    """Return the current Windows thread desktop name (e.g. 'Default' or 'exebox-...')."""
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


def is_isolated_windows_desktop(desktop_name: str) -> bool:
    """True when running on a non-Default Windows desktop such as exebox-*."""
    if not desktop_name:
        return False
    return desktop_name.strip().lower() != "default"


def filetime_to_iso_utc(filetime_ticks: int) -> str:
    """Lossless 100ns UTC ISO-8601 fingerprint from Win32 FILETIME."""
    unix_100ns = filetime_ticks - _FILETIME_UNIX_EPOCH_TICKS
    if unix_100ns < 0:
        return f"ft:{filetime_ticks}"
    secs, rem_100ns = divmod(unix_100ns, 10_000_000)
    base = dt.datetime.fromtimestamp(secs, tz=dt.timezone.utc)
    return f"{base.strftime('%Y-%m-%dT%H:%M:%S')}.{rem_100ns:07d}Z"


def now_windows_filetime() -> int:
    return int(time.time() * 10_000_000) + _FILETIME_UNIX_EPOCH_TICKS


def get_process_identity(pid: int) -> Optional[Dict[str, Any]]:
    """Query live process identity (pid, pid_name, pid_created_at, created_ft)."""
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
                    "created_ft": ft_ticks,
                }
            finally:
                k32.CloseHandle(hproc)
        except (AttributeError, OSError, ValueError):
            return None

    # POSIX fallback
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
            "created_ft": 0,
        }
    except OSError:
        return None


def verify_managed_process(
    pid: Any,
    expected_name: str,
    expected_created_at: str,
) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """Strictly verify PID + allowlisted process name + creation-time fingerprint."""
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False, "invalid_pid", None
    if pid_int <= 0:
        return False, "invalid_pid", None

    exp_name = str(expected_name or "").strip().lower()
    exp_created = str(expected_created_at or "").strip()
    allowed = ALLOWED_PID_NAMES_WIN if os.name == "nt" else ALLOWED_PID_NAMES_POSIX
    if not exp_name or exp_name not in allowed:
        return False, "disallowed_pid_name", None
    if not exp_created:
        return False, "missing_pid_created_at", None

    live = get_process_identity(pid_int)
    if not live:
        return False, "not_running", None

    live_name = str(live.get("pid_name") or "").strip().lower()
    live_created = str(live.get("pid_created_at") or "").strip()
    if live_name not in allowed or live_name != exp_name:
        return False, "pid_name_mismatch", live
    if live_created != exp_created:
        return False, "pid_created_at_mismatch", live
    return True, "verified_alive", live


def list_windows_processes() -> List[Tuple[int, int, str]]:
    """Fast Toolhelp32 process snapshot: list of (pid, parent_pid, exe_name)."""
    if os.name != "nt":
        return []
    try:
        import ctypes.wintypes as wt

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wt.DWORD),
                ("cntUsage", wt.DWORD),
                ("th32ProcessID", wt.DWORD),
                ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", wt.DWORD),
                ("cntThreads", wt.DWORD),
                ("th32ParentProcessID", wt.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wt.DWORD),
                ("szExeFile", wt.WCHAR * 260),
            ]

        k32 = ctypes.windll.kernel32
        k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
        k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
        k32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        k32.Process32FirstW.restype = wt.BOOL
        k32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        k32.Process32NextW.restype = wt.BOOL
        k32.CloseHandle.argtypes = [wt.HANDLE]
        k32.CloseHandle.restype = wt.BOOL

        snap = k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if not snap or snap == ctypes.c_void_p(-1).value:
            return []
        result: List[Tuple[int, int, str]] = []
        try:
            pe = PROCESSENTRY32W()
            pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            if k32.Process32FirstW(snap, ctypes.byref(pe)):
                while True:
                    result.append((int(pe.th32ProcessID), int(pe.th32ParentProcessID), str(pe.szExeFile)))
                    if not k32.Process32NextW(snap, ctypes.byref(pe)):
                        break
        finally:
            k32.CloseHandle(snap)
        return result
    except (AttributeError, OSError, ValueError):
        return []


def find_descendant_by_name(root_pid: int, target_names: Set[str]) -> Optional[Dict[str, Any]]:
    """Find a live process matching target_names in root_pid's descendant tree."""
    if os.name != "nt" or root_pid <= 0:
        return None
    procs = list_windows_processes()
    children_by_ppid: Dict[int, List[Tuple[int, str]]] = {}
    for pid, ppid, name in procs:
        children_by_ppid.setdefault(ppid, []).append((pid, name))

    lower_targets = {n.lower() for n in target_names}
    queue: List[int] = [root_pid]
    visited: Set[int] = {root_pid}
    while queue:
        cur = queue.pop(0)
        for child_pid, child_name in children_by_ppid.get(cur, []):
            if child_pid in visited:
                continue
            visited.add(child_pid)
            if child_name.lower() in lower_targets:
                ident = get_process_identity(child_pid)
                if ident:
                    return ident
            queue.append(child_pid)
    return None


def find_recent_codex_process(since_ft: int) -> Optional[Dict[str, Any]]:
    """Find the newest live codex.exe process created at or after since_ft."""
    if os.name != "nt":
        return None
    best: Optional[Dict[str, Any]] = None
    for pid, _, name in list_windows_processes():
        if name.lower() != "codex.exe":
            continue
        ident = get_process_identity(pid)
        if not ident:
            continue
        if ident.get("created_ft", 0) >= since_ft:
            if best is None or ident["created_ft"] > best["created_ft"]:
                best = ident
    return best


# ---------------------------------------------------------------------------
# Model catalog & config.toml probing
# ---------------------------------------------------------------------------

def parse_top_level_toml(config_path: Path) -> Dict[str, Any]:
    """Read top-level scalar settings from ~/.codex/config.toml (Python 3.8+ zero-dep)."""
    if not safe_is_file(config_path):
        return {}
    try:
        raw_text = config_path.read_text(encoding="utf-8-sig")
    except OSError:
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
        except (OSError, json.JSONDecodeError) as exc:
            cache_error = str(exc)

    models_all.sort(key=lambda m: (m["priority"], m["slug"]))
    visible_models = (
        list(models_all)
        if include_hidden
        else [m for m in models_all if m["visibility"] != "hide"]
    )
    if not visible_models and models_all:
        visible_models = list(models_all)

    return {
        "codex_home": str(codex_home),
        "models_cache_file": str(cache_path),
        "models_cache_exists": cache_exists,
        "models_cache_error": cache_error,
        "fetched_at": fetched_at,
        "client_version": client_version,
        "config_file": str(config_path),
        "config_exists": safe_is_file(config_path),
        "config_defaults": config_defaults,
        "models": visible_models,
        "all_models_by_slug": {m["slug"]: m for m in models_all},
    }


def emit_models_list(catalog: Dict[str, Any], as_json: bool) -> int:
    models = catalog["models"]
    has_models = bool(models)
    status = "MODELS_LISTED" if has_models else "NO_MODELS_CACHE"
    desktop = detect_windows_desktop()
    launch_mode = MODE_DAEMON_MANAGED if is_isolated_windows_desktop(desktop) else MODE_VISIBLE_WINDOW
    daemon_info = get_daemon_status(timeout=4.0) if launch_mode == MODE_DAEMON_MANAGED else {}

    payload: Dict[str, Any] = {
        "status": status,
        "codex_home": catalog["codex_home"],
        "models_cache_file": catalog["models_cache_file"],
        "fetched_at": catalog["fetched_at"],
        "client_version": catalog["client_version"],
        "config_file": catalog["config_file"],
        "config_defaults": catalog["config_defaults"],
        "desktop": desktop,
        "launch_mode": launch_mode,
        "models": models,
        "note": (
            "Ask the user to explicitly choose both model and reasoning effort from this real list. "
            "Never guess model names or silently fall back to config.toml defaults."
            if has_models
            else (
                "No cached model list found in models_cache.json. Do NOT guess model names — "
                "ask the user to verify Codex CLI login or run `codex` once to populate models_cache.json."
            )
        ),
    }
    if daemon_info:
        payload["daemon_status"] = daemon_info.get("status", "")
        payload["daemon_version"] = daemon_info.get("app_server_version", "")
    if catalog["models_cache_error"]:
        payload["error"] = catalog["models_cache_error"]

    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if has_models else 1

    print(f"STATUS: {status}")
    print(f"CODEX_HOME: {catalog['codex_home']}")
    print(f"MODELS_CACHE: {catalog['models_cache_file']}")
    if desktop:
        print(f"DESKTOP: {desktop} (launch_mode={launch_mode})")
    if daemon_info:
        ver_bit = f" v{daemon_info['app_server_version']}" if daemon_info.get("app_server_version") else ""
        print(f"DAEMON: {daemon_info.get('status', 'unknown')}{ver_bit}")
    if catalog["client_version"]:
        print(f"CLIENT_VERSION: {catalog['client_version']}")
    if catalog["fetched_at"]:
        print(f"FETCHED_AT: {catalog['fetched_at']}")
    if catalog["config_defaults"]:
        cfg_bits = ", ".join(f"{k}={v}" for k, v in catalog["config_defaults"].items())
        print(f"CONFIG_DEFAULTS (reference only; NOT used automatically): {cfg_bits}")
    if catalog["models_cache_error"]:
        print(f"CACHE_ERROR: {catalog['models_cache_error']}")
    if models:
        print("AVAILABLE_MODELS:")
        for idx, item in enumerate(models, start=1):
            efforts_str = ", ".join(item["supported_efforts"]) if item["supported_efforts"] else ", ".join(STANDARD_EFFORTS)
            default_eff = item["default_reasoning_level"]
            default_note = f" | default: {default_eff}" if default_eff else ""
            print(f"  {idx}. {item['slug']} ({item['display_name']}) | efforts: {efforts_str}{default_note}")
            if item["description"]:
                print(f"     {item['description']}")
    print(f"NOTE: {payload['note']}")
    return 0 if has_models else 1


# ---------------------------------------------------------------------------
# Command construction & desktop-aware launching
# ---------------------------------------------------------------------------

def ps_quote(value: str) -> str:
    """Single-quote a string safely for PowerShell."""
    return "'" + value.replace("'", "''") + "'"


def ps_arg(value: str) -> str:
    if value and re.fullmatch(r"[A-Za-z0-9._:/=-]+", value):
        return value
    return ps_quote(value)


def build_codex_argv(
    model: str,
    effort: str,
    sandbox: str = "",
    approval: str = "",
    init_prompt: str = "hi",
    resume_uuid: str = "",
) -> List[str]:
    """Assemble Codex CLI arguments (verified against `codex --help` & `codex resume --help`)."""
    argv = ["codex"]
    if resume_uuid:
        argv.append("resume")
    argv += ["-m", model, "-c", f"model_reasoning_effort={effort}"]
    if sandbox:
        argv += ["-s", sandbox]
    if approval:
        argv += ["-a", approval]
    if resume_uuid:
        argv.append(resume_uuid)
    elif init_prompt:
        argv.append(init_prompt)
    return argv


def build_powershell_codex_command(
    model: str,
    effort: str,
    sandbox: str = "",
    approval: str = "",
    init_prompt: str = "hi",
    resume_uuid: str = "",
) -> str:
    parts = ["codex"]
    if resume_uuid:
        parts.append("resume")
    parts += [
        "-m",
        ps_arg(model),
        "-c",
        ps_arg(f"model_reasoning_effort={effort}"),
    ]
    if sandbox:
        parts += ["-s", ps_arg(sandbox)]
    if approval:
        parts += ["-a", ps_arg(approval)]
    if resume_uuid:
        parts.append(ps_arg(resume_uuid))
    elif init_prompt:
        parts.append(ps_quote(init_prompt))
    return " ".join(parts)


def build_posix_codex_command(
    model: str,
    effort: str,
    sandbox: str = "",
    approval: str = "",
    init_prompt: str = "hi",
    resume_uuid: str = "",
) -> str:
    argv = build_codex_argv(model, effort, sandbox, approval, init_prompt, resume_uuid=resume_uuid)
    return " ".join(shlex.quote(arg) for arg in argv)


def sanitize_wt_dir(cwd: str) -> str:
    """Avoid Windows trailing backslash escaping issues and wt.exe ';' delimiter."""
    cleaned = cwd.strip()
    if len(cleaned) == 3 and cleaned[1] == ":" and cleaned[2] in ("\\", "/"):
        cleaned = cleaned[:2] + "\\."
    elif len(cleaned) > 3:
        cleaned = cleaned.rstrip("\\/")
    return cleaned.replace(";", "\\;")


def build_launch_plan(
    cwd: str,
    model: str,
    effort: str,
    sandbox: str = "",
    approval: str = "",
    init_prompt: str = "hi",
    resume_uuid: str = "",
    platform_override: str = "",
    desktop_override: Optional[str] = None,
    force_no_wt: bool = False,
) -> Dict[str, Any]:
    """Build cross-platform launch plan with Windows main/isolated desktop routing."""
    plat = platform_override or ("win32" if os.name == "nt" else sys.platform)

    if plat.startswith("win"):
        desktop = desktop_override if desktop_override is not None else detect_windows_desktop()
        ps_cmd = build_powershell_codex_command(
            model, effort, sandbox, approval, init_prompt, resume_uuid=resume_uuid
        )

        # Isolated sandbox desktop (e.g. exebox-*): host natively in Codex's persistent
        # app-server daemon (ZERO wt.exe, ZERO conhost.exe, survives Job Object closure).
        if is_isolated_windows_desktop(desktop):
            rpc_method = "thread/resume" if resume_uuid else "thread/start"
            target_bit = f" threadId={resume_uuid}" if resume_uuid else ""
            summary_cmd = (
                f"codex app-server proxy -> {rpc_method}"
                f"{target_bit} model={model} effort={effort}"
                f" sandbox={sandbox or 'default'} approval={approval or 'default'}"
            )
            return {
                "platform": "windows",
                "desktop": desktop,
                "mode": MODE_DAEMON_MANAGED,
                "launcher": "app-server-daemon",
                "rpc_method": rpc_method,
                "codex_cmd": summary_cmd,
                "launch_argv": ["codex", "app-server", "proxy"],
                "launch_cmd_display": summary_cmd,
            }

        # Main desktop ('Default' or unknown): visible standalone window via wt.exe
        wt_exe = None if force_no_wt else (shutil.which("wt.exe") or shutil.which("wt"))
        if wt_exe:
            wt_cwd = sanitize_wt_dir(cwd)
            wt_ps_cmd = ps_cmd.replace(";", "\\;")
            argv = [
                "wt.exe",
                "-w",
                "new",
                "-d",
                wt_cwd,
                "powershell",
                "-NoExit",
                "-Command",
                wt_ps_cmd,
            ]
            display = f'wt.exe -w new -d "{cwd}" powershell -NoExit -Command "{ps_cmd}"'
            return {
                "platform": "windows",
                "desktop": desktop or "Default",
                "mode": MODE_VISIBLE_WINDOW,
                "launcher": "wt.exe",
                "codex_cmd": ps_cmd,
                "launch_argv": argv,
                "launch_cmd_display": display,
            }

        sp_cmd = (
            f"Start-Process powershell -WorkingDirectory {ps_quote(cwd)} "
            f"-ArgumentList @('-NoExit', '-Command', {ps_quote(ps_cmd)})"
        )
        argv = ["powershell", "-NoProfile", "-Command", sp_cmd]
        return {
            "platform": "windows",
            "desktop": desktop or "Default",
            "mode": MODE_VISIBLE_WINDOW,
            "launcher": "powershell-start-process",
            "codex_cmd": ps_cmd,
            "launch_argv": argv,
            "launch_cmd_display": f'powershell -NoProfile -Command "{sp_cmd}"',
        }

    posix_codex = build_posix_codex_command(
        model, effort, sandbox, approval, init_prompt, resume_uuid=resume_uuid
    )

    if plat == "darwin":
        shell_line = f"cd {shlex.quote(cwd)} && {posix_codex}"
        escaped_applescript = shell_line.replace("\\", "\\\\").replace('"', '\\"')
        argv = [
            "osascript",
            "-e",
            f'tell application "Terminal" to do script "{escaped_applescript}"',
            "-e",
            'tell application "Terminal" to activate',
        ]
        return {
            "platform": "macos",
            "desktop": "",
            "mode": MODE_VISIBLE_WINDOW,
            "launcher": "osascript-terminal",
            "codex_cmd": posix_codex,
            "launch_argv": argv,
            "launch_cmd_display": " ".join(shlex.quote(x) for x in argv),
        }

    # Linux / other POSIX terminals
    linux_shell_cmd = f'cd {shlex.quote(cwd)} && {posix_codex}; exec "${{SHELL:-bash}}"'
    linux_candidates: List[Tuple[str, List[str]]] = [
        ("x-terminal-emulator", ["x-terminal-emulator", "-e", "bash", "-lc", linux_shell_cmd]),
        ("gnome-terminal", ["gnome-terminal", "--working-directory", cwd, "--", "bash", "-lc", linux_shell_cmd]),
        ("konsole", ["konsole", "--workdir", cwd, "-e", "bash", "-lc", linux_shell_cmd]),
        ("xfce4-terminal", ["xfce4-terminal", "--working-directory", cwd, "-e", f"bash -lc {shlex.quote(linux_shell_cmd)}"]),
        ("kitty", ["kitty", "--directory", cwd, "bash", "-lc", linux_shell_cmd]),
        ("alacritty", ["alacritty", "--working-directory", cwd, "-e", "bash", "-lc", linux_shell_cmd]),
        ("wezterm", ["wezterm", "start", "--cwd", cwd, "--", "bash", "-lc", linux_shell_cmd]),
        ("xterm", ["xterm", "-e", "bash", "-lc", linux_shell_cmd]),
    ]
    for binary, argv in linux_candidates:
        if shutil.which(binary):
            return {
                "platform": "linux",
                "desktop": "",
                "mode": MODE_VISIBLE_WINDOW,
                "launcher": binary,
                "codex_cmd": posix_codex,
                "launch_argv": argv,
                "launch_cmd_display": " ".join(shlex.quote(x) for x in argv),
            }

    return {
        "platform": "linux",
        "desktop": "",
        "mode": MODE_VISIBLE_WINDOW,
        "launcher": "",
        "codex_cmd": posix_codex,
        "launch_argv": [],
        "launch_cmd_display": "",
    }


def launch_sandbox_background(
    launch_argv: List[str],
    cwd: str,
) -> Tuple[subprocess.Popen[Any], Optional[Dict[str, Any]]]:
    """Launch conhost.exe powershell.exe safely in an isolated desktop without wt.exe.

    Strictly avoids DETACHED_PROCESS, CREATE_NO_WINDOW, and stdin=DEVNULL (which
    causes powershell.exe -NoExit to see EOF and exit immediately). Passing only
    close_fds=True + CREATE_NEW_PROCESS_GROUP lets conhost.exe attach a real Win32
    console for Codex CLI without leaking parent pipes or triggering WindowsTerminal.exe.
    """
    proc = subprocess.Popen(
        launch_argv,
        cwd=cwd or None,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        close_fds=True,
    )
    ident = get_process_identity(proc.pid)
    return proc, ident


def launch_terminal_window(
    launch_argv: List[str],
    cwd: str,
) -> Tuple[str, Optional[subprocess.Popen[Any]], Optional[Dict[str, Any]]]:
    """Launch an independent, visible terminal window for Codex CLI (main desktop)."""
    if not launch_argv:
        raise OSError("Empty terminal launch command.")

    if os.name == "nt":
        sw_shownormal = 1
        exe = launch_argv[0]
        params = subprocess.list2cmdline(launch_argv[1:]) if len(launch_argv) > 1 else None
        try:
            shell32 = ctypes.windll.shell32
            shell_exec = shell32.ShellExecuteW
            shell_exec.argtypes = [
                ctypes.c_void_p,
                ctypes.c_wchar_p,
                ctypes.c_wchar_p,
                ctypes.c_wchar_p,
                ctypes.c_wchar_p,
                ctypes.c_int,
            ]
            shell_exec.restype = ctypes.c_void_p
            rc = int(shell_exec(None, "open", exe, params, cwd or None, sw_shownormal) or 0)
            if rc > 32:
                return "shellexecute", None, None
        except (AttributeError, OSError, ValueError):
            pass

        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0x00000001)
        startupinfo.wShowWindow = sw_shownormal
        creationflags = (
            getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
        proc = subprocess.Popen(
            launch_argv,
            cwd=cwd or None,
            startupinfo=startupinfo,
            creationflags=creationflags,
            close_fds=True,
        )
        return "popen-new-console", proc, get_process_identity(proc.pid)

    proc = subprocess.Popen(
        launch_argv,
        cwd=cwd or None,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return "popen-posix", proc, get_process_identity(proc.pid)


# ---------------------------------------------------------------------------
# Session cache, history lookup & safe PID termination
# ---------------------------------------------------------------------------

def read_existing_cache(cache_path: Path, expected_norm_cwd: str) -> Optional[Dict[str, Any]]:
    if not safe_is_file(cache_path):
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


def clear_cache_pid_fields(cache_path: Path) -> None:
    if not safe_is_file(cache_path):
        return
    try:
        loaded = json.loads(cache_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(loaded, dict):
        return
    for key in ("pid", "pid_name", "pid_created_at"):
        loaded.pop(key, None)
    loaded["updated_at"] = utc_now_iso()
    try:
        cache_path.write_text(
            json.dumps(loaded, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def mark_daemon_session_stopped(cache_path: Path) -> None:
    if not safe_is_file(cache_path):
        return
    try:
        loaded = json.loads(cache_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(loaded, dict):
        return
    loaded["stopped"] = True
    loaded["updated_at"] = utc_now_iso()
    try:
        cache_path.write_text(
            json.dumps(loaded, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def update_cache_launch_metadata(
    cache_path: Path,
    thread_id: str,
    cwd: str,
    model: str,
    effort: str,
    sandbox: str,
    approval: str,
    mode: str,
    desktop: str = "",
    proc_ident: Optional[Dict[str, Any]] = None,
    session_file: str = "",
    session_timestamp: str = "",
    daemon_version: str = "",
) -> None:
    existing: Dict[str, Any] = {}
    if safe_is_file(cache_path):
        try:
            loaded = json.loads(cache_path.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, json.JSONDecodeError):
            existing = {}

    existing["thread_id"] = thread_id
    existing["cwd"] = existing.get("cwd") or cwd
    if session_file:
        existing["session_file"] = session_file
    if session_timestamp:
        existing["session_timestamp"] = session_timestamp
    existing["model"] = model
    existing["reasoning_effort"] = effort
    if sandbox:
        existing["sandbox"] = sandbox
    else:
        existing.pop("sandbox", None)
    if approval:
        existing["approval"] = approval
    else:
        existing.pop("approval", None)
    existing["mode"] = mode
    if desktop:
        existing["desktop"] = desktop
    else:
        existing.pop("desktop", None)
    if daemon_version:
        existing["daemon_version"] = daemon_version
    elif mode != MODE_DAEMON_MANAGED:
        existing.pop("daemon_version", None)
    existing.pop("stopped", None)

    if mode != MODE_DAEMON_MANAGED and proc_ident and proc_ident.get("pid"):
        existing["pid"] = int(proc_ident["pid"])
        existing["pid_name"] = str(proc_ident.get("pid_name") or "")
        existing["pid_created_at"] = str(proc_ident.get("pid_created_at") or "")
    else:
        for key in ("pid", "pid_name", "pid_created_at"):
            existing.pop(key, None)

    existing["launched_by"] = "start_codex_window.py"
    existing["updated_at"] = utc_now_iso()

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def terminate_verified_pid(pid: int, pid_name: str, pid_created_at: str) -> Tuple[str, str]:
    """Terminate a process tree ONLY after verifying PID + name + creation-time fingerprint."""
    ok, reason, _ = verify_managed_process(pid, pid_name, pid_created_at)
    if not ok:
        if reason == "not_running":
            return "ALREADY_EXITED", f"PID {pid} ({pid_name}) is no longer running."
        return (
            "SKIPPED_MISMATCH",
            f"Refused to kill PID {pid}: fingerprint verification failed ({reason}).",
        )

    if os.name == "nt":
        proc = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if get_process_identity(pid) is None:
                break
            time.sleep(0.15)
        if get_process_identity(pid) is None:
            return "STOPPED", f"Terminated process tree for PID {pid} ({pid_name})."
        err_msg = (proc.stderr or proc.stdout).strip()
        return "STOP_FAILED", f"taskkill failed for PID {pid}: {err_msg}"

    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as exc:
            return "STOP_FAILED", str(exc)
    return "STOPPED", f"Terminated PID {pid} ({pid_name})."


def stop_cached_session(
    cache_path: Path,
    expected_norm_cwd: str,
    only_sandbox_background: bool = False,
    skip_thread_id: str = "",
) -> Dict[str, Any]:
    """Safely stop the managed session recorded in .agent-collab/session.json.

    - DAEMON_MANAGED: gracefully interrupts any active turn and unsubscribes ONLY
      this project's thread via `thread/unsubscribe` (never stops the shared daemon
      and never kills any OS processes).
    - SANDBOX_BACKGROUND (legacy): terminates ONLY after verifying PID + process name +
      creation-time fingerprint.
    - VISIBLE_WINDOW: untouched during reconfigure cleanup (left for user to close).
    """
    cached = read_existing_cache(cache_path, expected_norm_cwd)
    if not cached:
        return {
            "status": "NO_ACTIVE_SESSION",
            "cache_file": str(cache_path),
            "note": "No cached .agent-collab/session.json found for this project.",
        }

    mode = str(cached.get("mode") or "").upper()
    cached_tid = str(cached.get("thread_id") or "").strip()

    if mode == MODE_DAEMON_MANAGED:
        if skip_thread_id and cached_tid == skip_thread_id:
            return {
                "status": "SKIPPED_SAME_THREAD",
                "thread_id": cached_tid,
                "mode": mode,
                "cache_file": str(cache_path),
                "note": "Target resume UUID matches cached DAEMON_MANAGED thread; keeping active binding.",
            }
        if not cached_tid:
            mark_daemon_session_stopped(cache_path)
            return {
                "status": "ALREADY_EXITED",
                "mode": mode,
                "cache_file": str(cache_path),
                "note": "No thread_id recorded in cached DAEMON_MANAGED session.",
            }
        daemon_info = get_daemon_status(timeout=5.0)
        if not daemon_info.get("running"):
            mark_daemon_session_stopped(cache_path)
            return {
                "status": "ALREADY_EXITED",
                "thread_id": cached_tid,
                "mode": mode,
                "cache_file": str(cache_path),
                "note": "Managed Codex daemon is not running; marked cached thread as stopped.",
            }
        try:
            with CodexDaemonClient(timeout=10.0, socket_path=daemon_info.get("socket_path", "")) as client:
                interrupted = client.interrupt_active_turn(cached_tid)
                unsub_res = client.unsubscribe_thread(cached_tid)
                unsub_status = str(unsub_res.get("status") or "unsubscribed")
        except DaemonRPCError as exc:
            return {
                "status": "STOP_FAILED",
                "thread_id": cached_tid,
                "mode": mode,
                "cache_file": str(cache_path),
                "note": f"Failed to unsubscribe daemon thread {cached_tid}: {exc}",
            }
        mark_daemon_session_stopped(cache_path)
        return {
            "status": "STOPPED",
            "thread_id": cached_tid,
            "mode": mode,
            "unsubscribe_status": unsub_status,
            "cache_file": str(cache_path),
            "note": (
                f"Gracefully unsubscribed daemon thread {cached_tid} (status={unsub_status}, "
                f"turn_interrupted={interrupted}). Shared daemon left running."
            ),
        }

    if only_sandbox_background and mode != MODE_SANDBOX_BACKGROUND:
        return {
            "status": "SKIPPED_VISIBLE_WINDOW",
            "thread_id": cached_tid,
            "mode": mode,
            "cache_file": str(cache_path),
            "note": "Existing session is VISIBLE_WINDOW; leaving visible window untouched.",
        }

    raw_pid = cached.get("pid")
    pid_name = str(cached.get("pid_name") or "")
    pid_created_at = str(cached.get("pid_created_at") or "")

    if raw_pid in (None, ""):
        return {
            "status": "NO_ACTIVE_PID",
            "thread_id": cached.get("thread_id", ""),
            "mode": mode,
            "cache_file": str(cache_path),
            "note": "No managed PID recorded in session.json.",
        }

    try:
        pid_int = int(raw_pid)
    except (TypeError, ValueError):
        clear_cache_pid_fields(cache_path)
        return {
            "status": "SKIPPED_MISMATCH",
            "thread_id": cached.get("thread_id", ""),
            "cache_file": str(cache_path),
            "note": f"Invalid PID value in session.json ({raw_pid!r}); cleared PID metadata.",
        }

    outcome, detail = terminate_verified_pid(pid_int, pid_name, pid_created_at)
    if outcome in ("STOPPED", "ALREADY_EXITED", "SKIPPED_MISMATCH"):
        clear_cache_pid_fields(cache_path)

    return {
        "status": outcome,
        "thread_id": cached.get("thread_id", ""),
        "mode": mode,
        "pid": pid_int,
        "pid_name": pid_name,
        "pid_created_at": pid_created_at,
        "cache_file": str(cache_path),
        "note": detail,
    }


def locate_session_jsonl(sessions_dir: Path, target_uuid: str) -> Optional[Dict[str, Any]]:
    """Verify that the rollout-*.jsonl file for target_uuid exists and return its meta."""
    if not safe_is_dir(sessions_dir) or not target_uuid:
        return None

    def _check_file(path: Path) -> Optional[Dict[str, Any]]:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                first_line = fh.readline().strip()
        except OSError:
            return None
        if not first_line:
            return None
        try:
            obj = json.loads(first_line)
        except json.JSONDecodeError:
            return None
        if not isinstance(obj, dict):
            return None
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else obj
        tid = str(
            payload.get("id")
            or payload.get("session_id")
            or obj.get("id")
            or obj.get("session_id")
            or ""
        ).strip()
        if tid != target_uuid:
            return None
        cwd = str(payload.get("cwd") or obj.get("cwd") or "").strip()
        if not cwd:
            roots = payload.get("runtime_workspace_roots")
            if isinstance(roots, list) and roots:
                cwd = str(roots[0]).strip()
        ts = str(payload.get("timestamp") or obj.get("timestamp") or "").strip()
        return {
            "thread_id": tid,
            "cwd": cwd,
            "session_file": str(path),
            "session_timestamp": ts,
        }

    # Fast path: filename includes UUID
    try:
        for path in sessions_dir.rglob(f"*{target_uuid}*.jsonl"):
            found = _check_file(path)
            if found:
                return found
        # Fallback: scan all rollout-*.jsonl files (supports custom filenames)
        for path in sessions_dir.rglob("rollout-*.jsonl"):
            found = _check_file(path)
            if found:
                return found
    except OSError:
        return None
    return None


def wait_for_resumed_codex_ready(
    mode: str,
    launcher_proc: Optional[subprocess.Popen[Any]],
    launch_ft: int,
    timeout: float,
    interval: float,
    ready_delay: float = 2.5,
) -> Tuple[bool, Optional[Dict[str, Any]], str]:
    """Wait until codex.exe is alive in the resumed process tree, then briefly settle."""
    deadline = time.monotonic() + max(0.5, timeout)

    if os.name != "nt":
        time.sleep(min(ready_delay, max(0.2, timeout)))
        if launcher_proc is not None and launcher_proc.poll() is not None:
            return False, None, "Launcher exited before Codex became ready."
        ident = get_process_identity(launcher_proc.pid) if launcher_proc else None
        return True, ident, "Resumed session started."

    codex_ident: Optional[Dict[str, Any]] = None
    while True:
        if mode == MODE_SANDBOX_BACKGROUND and launcher_proc is not None:
            if launcher_proc.poll() is not None:
                return False, None, "Background conhost.exe exited prematurely while resuming."
            codex_ident = find_descendant_by_name(launcher_proc.pid, {"codex.exe"})
        else:
            codex_ident = find_recent_codex_process(launch_ft)

        if codex_ident:
            break

        now = time.monotonic()
        if now >= deadline:
            return False, None, f"codex.exe did not start within {timeout:g}s."
        time.sleep(min(interval, max(0.1, deadline - now)))

    # Brief settle window so Codex TUI finishes loading history before `codex queue` runs
    settle_until = time.monotonic() + max(0.0, ready_delay)
    while time.monotonic() < settle_until:
        if mode == MODE_SANDBOX_BACKGROUND and launcher_proc is not None:
            if launcher_proc.poll() is not None:
                return False, None, "Background conhost.exe exited during resume startup."
        time.sleep(min(0.25, max(0.05, settle_until - time.monotonic())))

    # Re-verify codex.exe is still alive after settle
    alive_ok, _, _ = verify_managed_process(
        codex_ident["pid"], codex_ident["pid_name"], codex_ident["pid_created_at"]
    )
    if not alive_ok:
        return False, None, "codex.exe exited shortly after starting resume."
    return True, codex_ident, "Resumed Codex process verified alive and ready."


def poll_new_thread_uuid(
    find_script: Path,
    resolved_cwd: str,
    since_ts: float,
    timeout: float,
    interval: float,
    sessions_dir: str = "",
    cache_file: str = "",
    launcher_proc: Optional[subprocess.Popen[Any]] = None,
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Poll find_codex_thread.py --refresh --since <ts> until new UUID is captured or timeout."""
    deadline = time.monotonic() + max(0.0, timeout)
    base_cmd = [
        sys.executable,
        str(find_script),
        "--cwd",
        resolved_cwd,
        "--refresh",
        "--since",
        f"{since_ts:.3f}",
        "--json",
    ]
    if sessions_dir:
        base_cmd += ["--sessions-dir", sessions_dir]
    if cache_file:
        base_cmd += ["--cache-file", cache_file]

    while True:
        if launcher_proc is not None and launcher_proc.poll() is not None:
            return None, {"early_exit": True}
        try:
            proc = subprocess.run(
                base_cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if proc.stdout.strip():
                data = json.loads(proc.stdout)
                if isinstance(data, dict):
                    status = data.get("status")
                    if status in ("AUTO_SELECTED", "AUTO_SELECTED_STALE", "SELECTED") and data.get("thread_id"):
                        return str(data["thread_id"]).strip(), data
                    if status == "MULTIPLE_MATCHES":
                        candidates = data.get("candidates") or []
                        if candidates and isinstance(candidates[0], dict) and candidates[0].get("thread_id"):
                            chosen_id = str(candidates[0]["thread_id"]).strip()
                            sel_cmd = [
                                sys.executable,
                                str(find_script),
                                "--cwd",
                                resolved_cwd,
                                "--select",
                                chosen_id,
                                "--json",
                            ]
                            if sessions_dir:
                                sel_cmd += ["--sessions-dir", sessions_dir]
                            if cache_file:
                                sel_cmd += ["--cache-file", cache_file]
                            subprocess.run(
                                sel_cmd,
                                capture_output=True,
                                text=True,
                                encoding="utf-8",
                                errors="replace",
                                check=False,
                            )
                            return chosen_id, data
        except (OSError, json.JSONDecodeError):
            pass

        now = time.monotonic()
        if now >= deadline:
            return None, None
        time.sleep(min(interval, max(0.05, deadline - now)))


def emit_result(data: Dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return

    print(f"STATUS: {data['status']}")
    field_order = (
        ("thread_id", "THREAD_ID"),
        ("cwd", "CWD"),
        ("platform", "PLATFORM"),
        ("desktop", "DESKTOP"),
        ("mode", "MODE"),
        ("launcher", "LAUNCHER"),
        ("daemon_version", "DAEMON_VERSION"),
        ("unsubscribe_status", "UNSUBSCRIBE_STATUS"),
        ("pid", "PID"),
        ("pid_name", "PID_NAME"),
        ("pid_created_at", "PID_CREATED_AT"),
        ("codex_pid", "CODEX_PID"),
        ("model", "MODEL"),
        ("effort", "EFFORT"),
        ("sandbox", "SANDBOX"),
        ("approval", "APPROVAL"),
        ("codex_cmd", "CODEX_CMD"),
        ("launch_cmd", "LAUNCH_CMD"),
        ("cache_file", "CACHE_FILE"),
        ("elapsed_sec", "ELAPSED_SEC"),
        ("cleanup_status", "CLEANUP_STATUS"),
        ("note", "NOTE"),
        ("hint", "HINT"),
    )
    for key, label in field_order:
        val = data.get(key)
        if val not in (None, ""):
            print(f"{label}: {val}")


def main() -> int:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(
        description=(
            "Probe real Codex models (--list-models), stop a managed background Codex (--stop), "
            "or launch/resume a Codex CLI session with explicit --model and --effort."
        )
    )
    parser.add_argument(
        "--list-models",
        action="store_true",
        help="Read ~/.codex/models_cache.json and ~/.codex/config.toml to print real available models & efforts.",
    )
    parser.add_argument(
        "--include-hidden",
        action="store_true",
        help="Include models with visibility='hide' when running --list-models.",
    )
    parser.add_argument(
        "--stop",
        action="store_true",
        help="Safely verify PID + process name + creation time fingerprint and terminate the managed background Codex tree.",
    )
    parser.add_argument(
        "--resume",
        default="",
        help="Resume an existing historical Codex session UUID on demand (requires --model and --effort).",
    )
    parser.add_argument(
        "--cwd",
        default=os.getcwd(),
        help="Target project root directory (default: current working directory).",
    )
    parser.add_argument(
        "--model",
        default="",
        help="Required when launching/resuming: exact Codex model slug (passed as -m <model>). Never defaulted.",
    )
    parser.add_argument(
        "--effort",
        default="",
        help=(
            "Required when launching/resuming: reasoning effort, e.g. low|medium|high|xhigh|max|ultra "
            "(validated dynamically against models_cache.json; passed as -c model_reasoning_effort=<effort>)."
        ),
    )
    parser.add_argument(
        "--sandbox",
        choices=SANDBOX_CHOICES,
        default="",
        help="Optional Codex sandbox policy passed as -s <sandbox>.",
    )
    parser.add_argument(
        "--approval",
        choices=APPROVAL_CHOICES,
        default="",
        help="Optional Codex approval policy passed as -a <approval>.",
    )
    parser.add_argument(
        "--force-new",
        action="store_true",
        help="Safely stop any live SANDBOX_BACKGROUND session, open a fresh Codex session, and overwrite session.json.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=25.0,
        help="Seconds to wait for the Codex session UUID / resumed process to become ready (default: 25).",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Polling interval in seconds (default: 1.0).",
    )
    parser.add_argument(
        "--ready-delay",
        type=float,
        default=2.5,
        help="Seconds to settle after confirming codex.exe is alive during --resume (default: 2.5).",
    )
    parser.add_argument(
        "--init-prompt",
        default="hi",
        help="Initial message sent to initialize a brand-new Codex session log (default: 'hi'; unused with --resume).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate arguments and print the final launch/resume command without spawning processes.",
    )
    parser.add_argument(
        "--codex-home",
        default="",
        help="Override Codex home directory (default: $CODEX_HOME or ~/.codex).",
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
        "--json",
        action="store_true",
        help="Output machine-readable JSON.",
    )
    args = parser.parse_args()

    cwd_path = Path(args.cwd).expanduser()
    try:
        resolved_cwd = str(cwd_path.resolve())
    except OSError:
        resolved_cwd = os.path.abspath(str(cwd_path))
    target_norm_cwd = normalize_path(resolved_cwd)
    cache_path = (
        Path(args.cache_file).expanduser()
        if args.cache_file
        else Path(resolved_cwd) / ".agent-collab" / "session.json"
    )

    if args.stop:
        res = stop_cached_session(cache_path, target_norm_cwd, only_sandbox_background=False)
        res["cwd"] = resolved_cwd
        emit_result(res, args.json)
        return 0 if res["status"] != "STOP_FAILED" else 1

    codex_home = resolve_codex_home(args.codex_home)
    catalog = load_models_catalog(codex_home, include_hidden=args.include_hidden)

    if args.list_models:
        return emit_models_list(catalog, args.json)

    # Hard validation: --model and --effort are strictly mandatory; never fall back to config.toml
    model = args.model.strip()
    effort = args.effort.strip().lower()
    resume_uuid = args.resume.strip()

    missing = []
    if not model:
        missing.append("--model")
    if not effort:
        missing.append("--effort")
    if missing:
        print(
            f"ERROR: Missing required argument(s): {', '.join(missing)}. "
            "Both --model and --effort MUST be explicitly chosen by the user; "
            "this script never falls back to ~/.codex/config.toml defaults. "
            "Run with --list-models first and ask the user to choose.",
            file=sys.stderr,
        )
        return 2

    matched_model = catalog["all_models_by_slug"].get(model)
    if matched_model and matched_model.get("supported_efforts"):
        supported = matched_model["supported_efforts"]
        if effort not in supported:
            print(
                f"ERROR: Effort '{effort}' is not supported by model '{model}'. "
                f"Supported reasoning levels for '{model}': {', '.join(supported)}.",
                file=sys.stderr,
            )
            return 2
    else:
        if effort not in KNOWN_EFFORTS:
            print(
                f"ERROR: Invalid --effort '{effort}'. "
                f"Expected one of: {', '.join(STANDARD_EFFORTS)} (or model-supported levels: {', '.join(KNOWN_EFFORTS)}).",
                file=sys.stderr,
            )
            return 2
        if catalog["all_models_by_slug"] and model != catalog["config_defaults"].get("model"):
            print(
                f"WARNING: Model '{model}' was not found in {catalog['models_cache_file']}. "
                "Verify with --list-models to avoid launching an invalid model.",
                file=sys.stderr,
            )

    if args.timeout < 0 or args.interval <= 0 or args.ready_delay < 0:
        print("ERROR: --timeout/--ready-delay must be >= 0 and --interval must be > 0.", file=sys.stderr)
        return 2

    if not args.dry_run and not safe_is_dir(Path(resolved_cwd)):
        print(f"ERROR: Target --cwd directory does not exist: {resolved_cwd}", file=sys.stderr)
        return 2

    sessions_dir_path = (
        Path(args.sessions_dir).expanduser()
        if args.sessions_dir
        else codex_home / "sessions"
    )
    sessions_dir_arg = args.sessions_dir or (str(sessions_dir_path) if args.codex_home else "")

    plan = build_launch_plan(
        cwd=resolved_cwd,
        model=model,
        effort=effort,
        sandbox=args.sandbox,
        approval=args.approval,
        init_prompt=args.init_prompt,
        resume_uuid=resume_uuid,
    )

    resumed_meta: Optional[Dict[str, Any]] = None
    if resume_uuid:
        resumed_meta = locate_session_jsonl(sessions_dir_path, resume_uuid)
        if not resumed_meta and plan["mode"] == MODE_DAEMON_MANAGED:
            # Handle a DAEMON_MANAGED thread created via thread/start before its first
            # `codex queue` call materializes `rollout-*.jsonl` on disk.
            loaded_in_daemon, _, _ = check_daemon_thread_loaded(resume_uuid, timeout=6.0)
            if loaded_in_daemon:
                resumed_meta = {
                    "thread_id": resume_uuid,
                    "cwd": resolved_cwd,
                    "session_file": "",
                    "session_timestamp": "",
                }
        if not resumed_meta:
            print(
                f"ERROR: Historical session log (.jsonl) for UUID '{resume_uuid}' was not found under {sessions_dir_path}. "
                "Run `find_codex_thread.py --cwd \"<REPO_ROOT>\" --list-history` to inspect valid session UUIDs.",
                file=sys.stderr,
            )
            return 2

    if not plan["launch_argv"]:
        print(
            "ERROR: No supported terminal emulator found to launch an independent Codex CLI window.",
            file=sys.stderr,
        )
        return 1

    if args.dry_run:
        emit_result(
            {
                "status": "DRY_RUN",
                "action": "resume" if resume_uuid else "start",
                "thread_id": resume_uuid,
                "cwd": resolved_cwd,
                "platform": plan["platform"],
                "desktop": plan.get("desktop", ""),
                "mode": plan["mode"],
                "launcher": plan["launcher"],
                "model": model,
                "effort": effort,
                "sandbox": args.sandbox,
                "approval": args.approval,
                "codex_cmd": plan["codex_cmd"],
                "launch_cmd": plan["launch_cmd_display"],
                "launch_argv": plan["launch_argv"],
                "cache_file": str(cache_path),
                "force_new": bool(args.force_new),
            },
            args.json,
        )
        return 0

    # Reuse check for normal start (without --force-new and without --resume)
    if not resume_uuid and not args.force_new:
        existing = read_existing_cache(cache_path, target_norm_cwd)
        if existing:
            existing_mode = str(existing.get("mode") or "").upper()
            still_usable = True
            if existing_mode == MODE_DAEMON_MANAGED:
                if existing.get("stopped") is True:
                    still_usable = False
                else:
                    still_usable, _, _ = check_daemon_thread_loaded(str(existing.get("thread_id") or ""))
            elif existing_mode == MODE_SANDBOX_BACKGROUND:
                ok_alive, _, _ = verify_managed_process(
                    existing.get("pid"),
                    str(existing.get("pid_name") or ""),
                    str(existing.get("pid_created_at") or ""),
                )
                still_usable = ok_alive
            if still_usable:
                emit_result(
                    {
                        "status": "ALREADY_BOUND",
                        "thread_id": existing["thread_id"],
                        "cwd": existing.get("cwd", resolved_cwd),
                        "mode": existing_mode or MODE_VISIBLE_WINDOW,
                        "daemon_version": existing.get("daemon_version", ""),
                        "pid": existing.get("pid"),
                        "pid_name": existing.get("pid_name", ""),
                        "pid_created_at": existing.get("pid_created_at", ""),
                        "model": existing.get("model", ""),
                        "effort": existing.get("reasoning_effort", ""),
                        "cache_file": str(cache_path),
                        "note": (
                            "An active Codex session is already cached for this project. "
                            "Reuse THREAD_ID directly or rerun with --force-new to open a new session and rebind."
                        ),
                    },
                    args.json,
                )
                return 0

    # Safe pre-launch cleanup of any previously alive background/daemon session
    cleanup_status = ""
    if args.force_new or resume_uuid:
        cleanup = stop_cached_session(
            cache_path,
            target_norm_cwd,
            only_sandbox_background=True,
            skip_thread_id=resume_uuid,
        )
        cleanup_status = cleanup.get("status", "")
        if cleanup_status == "STOP_FAILED":
            print(f"WARNING: Failed to stop previous background Codex: {cleanup.get('note')}", file=sys.stderr)

    start_mono = time.monotonic()

    # Native DAEMON_MANAGED branch (isolated sandbox desktop: zero terminal windows, zero PID killing)
    if plan["mode"] == MODE_DAEMON_MANAGED:
        try:
            daemon_info = ensure_daemon_running(timeout=min(max(args.timeout, 5.0), 15.0))
        except DaemonUnavailableError as exc:
            elapsed = round(time.monotonic() - start_mono, 1)
            emit_result(
                {
                    "status": "DAEMON_UNAVAILABLE",
                    "thread_id": resume_uuid,
                    "cwd": resolved_cwd,
                    "platform": plan["platform"],
                    "desktop": plan.get("desktop", ""),
                    "mode": MODE_DAEMON_MANAGED,
                    "launcher": plan["launcher"],
                    "model": model,
                    "effort": effort,
                    "sandbox": args.sandbox,
                    "approval": args.approval,
                    "cache_file": str(cache_path),
                    "elapsed_sec": elapsed,
                    "note": str(exc),
                    "hint": exc.hint,
                },
                args.json,
            )
            return 1

        try:
            with CodexDaemonClient(
                timeout=max(10.0, args.timeout),
                socket_path=daemon_info.get("socket_path", ""),
            ) as client:
                if resume_uuid:
                    rpc_info = client.resume_thread(
                        thread_id=resume_uuid,
                        cwd=resolved_cwd,
                        model=model,
                        effort=effort,
                        sandbox=args.sandbox,
                        approval=args.approval,
                        exclude_turns=True,
                    )
                    target_tid = rpc_info["thread_id"]
                    loaded = client.list_loaded_threads()
                    if target_tid not in loaded:
                        raise DaemonRPCError(
                            f"Thread {target_tid} was not present in thread/loaded/list after thread/resume."
                        )
                    elapsed = round(time.monotonic() - start_mono, 1)
                    update_cache_launch_metadata(
                        cache_path=cache_path,
                        thread_id=target_tid,
                        cwd=resolved_cwd,
                        model=model,
                        effort=effort,
                        sandbox=args.sandbox,
                        approval=args.approval,
                        mode=MODE_DAEMON_MANAGED,
                        desktop=plan.get("desktop", ""),
                        session_file=rpc_info.get("session_file") or (resumed_meta["session_file"] if resumed_meta else ""),
                        session_timestamp=resumed_meta["session_timestamp"] if resumed_meta else "",
                        daemon_version=daemon_info.get("app_server_version", ""),
                    )
                    emit_result(
                        {
                            "status": "RESUMED_AND_BOUND",
                            "thread_id": target_tid,
                            "cwd": resolved_cwd,
                            "platform": plan["platform"],
                            "desktop": plan.get("desktop", ""),
                            "mode": MODE_DAEMON_MANAGED,
                            "launcher": plan["launcher"],
                            "daemon_version": daemon_info.get("app_server_version", ""),
                            "model": rpc_info.get("model") or model,
                            "effort": rpc_info.get("reasoning_effort") or effort,
                            "sandbox": args.sandbox,
                            "approval": args.approval,
                            "codex_cmd": plan["codex_cmd"],
                            "launch_cmd": plan["launch_cmd_display"],
                            "cache_file": str(cache_path),
                            "elapsed_sec": elapsed,
                            "cleanup_status": cleanup_status,
                            "note": "Resumed thread natively in managed Codex daemon (excludeTurns=true; history preserved).",
                        },
                        args.json,
                    )
                    return 0

                rpc_info = client.start_thread(
                    cwd=resolved_cwd,
                    model=model,
                    effort=effort,
                    sandbox=args.sandbox,
                    approval=args.approval,
                )
                new_tid = rpc_info["thread_id"]
                loaded = client.list_loaded_threads()
                if new_tid not in loaded:
                    raise DaemonRPCError(
                        f"Thread {new_tid} was not present in thread/loaded/list after thread/start."
                    )
        except DaemonRPCError as exc:
            elapsed = round(time.monotonic() - start_mono, 1)
            emit_result(
                {
                    "status": "DAEMON_RPC_ERROR",
                    "thread_id": resume_uuid,
                    "cwd": resolved_cwd,
                    "platform": plan["platform"],
                    "desktop": plan.get("desktop", ""),
                    "mode": MODE_DAEMON_MANAGED,
                    "launcher": plan["launcher"],
                    "daemon_version": daemon_info.get("app_server_version", ""),
                    "model": model,
                    "effort": effort,
                    "sandbox": args.sandbox,
                    "approval": args.approval,
                    "cache_file": str(cache_path),
                    "elapsed_sec": elapsed,
                    "note": str(exc),
                },
                args.json,
            )
            return 1

        elapsed = round(time.monotonic() - start_mono, 1)
        update_cache_launch_metadata(
            cache_path=cache_path,
            thread_id=new_tid,
            cwd=resolved_cwd,
            model=model,
            effort=effort,
            sandbox=args.sandbox,
            approval=args.approval,
            mode=MODE_DAEMON_MANAGED,
            desktop=plan.get("desktop", ""),
            session_file=rpc_info.get("session_file", ""),
            session_timestamp=utc_now_iso(),
            daemon_version=daemon_info.get("app_server_version", ""),
        )
        emit_result(
            {
                "status": "STARTED_AND_BOUND",
                "thread_id": new_tid,
                "cwd": resolved_cwd,
                "platform": plan["platform"],
                "desktop": plan.get("desktop", ""),
                "mode": MODE_DAEMON_MANAGED,
                "launcher": plan["launcher"],
                "daemon_version": daemon_info.get("app_server_version", ""),
                "model": rpc_info.get("model") or model,
                "effort": rpc_info.get("reasoning_effort") or effort,
                "sandbox": args.sandbox,
                "approval": args.approval,
                "codex_cmd": plan["codex_cmd"],
                "launch_cmd": plan["launch_cmd_display"],
                "cache_file": str(cache_path),
                "elapsed_sec": elapsed,
                "cleanup_status": cleanup_status,
                "note": (
                    "Created thread natively in managed Codex daemon (0 terminal windows spawned; "
                    "rollout-*.jsonl will materialize on disk when the first message is queued)."
                ),
            },
            args.json,
        )
        return 0

    launch_ts = time.time()
    launch_ft = now_windows_filetime()

    launcher_proc: Optional[subprocess.Popen[Any]] = None
    managed_ident: Optional[Dict[str, Any]] = None

    try:
        if plan["mode"] == MODE_SANDBOX_BACKGROUND:
            launcher_proc, managed_ident = launch_sandbox_background(plan["launch_argv"], resolved_cwd)
        else:
            _, launcher_proc, managed_ident = launch_terminal_window(plan["launch_argv"], resolved_cwd)
    except OSError as exc:
        print(f"ERROR: Failed to launch Codex terminal process: {exc}", file=sys.stderr)
        return 1

    # Branch A: --resume <UUID> (verify live codex.exe in process tree + settle; no --since polling)
    if resume_uuid:
        ok_ready, codex_ident, detail = wait_for_resumed_codex_ready(
            mode=plan["mode"],
            launcher_proc=launcher_proc,
            launch_ft=launch_ft,
            timeout=args.timeout,
            interval=min(args.interval, 0.5),
            ready_delay=args.ready_delay,
        )
        elapsed = round(time.monotonic() - start_mono, 1)

        if ok_ready:
            stored_ident = managed_ident if plan["mode"] == MODE_SANDBOX_BACKGROUND else (codex_ident or managed_ident)
            update_cache_launch_metadata(
                cache_path=cache_path,
                thread_id=resume_uuid,
                cwd=resolved_cwd,
                model=model,
                effort=effort,
                sandbox=args.sandbox,
                approval=args.approval,
                mode=plan["mode"],
                desktop=plan.get("desktop", ""),
                proc_ident=stored_ident,
                session_file=resumed_meta["session_file"] if resumed_meta else "",
                session_timestamp=resumed_meta["session_timestamp"] if resumed_meta else "",
            )
            emit_result(
                {
                    "status": "RESUMED_AND_BOUND",
                    "thread_id": resume_uuid,
                    "cwd": resolved_cwd,
                    "platform": plan["platform"],
                    "desktop": plan.get("desktop", ""),
                    "mode": plan["mode"],
                    "launcher": plan["launcher"],
                    "pid": stored_ident["pid"] if stored_ident else None,
                    "pid_name": stored_ident["pid_name"] if stored_ident else "",
                    "pid_created_at": stored_ident["pid_created_at"] if stored_ident else "",
                    "codex_pid": codex_ident["pid"] if codex_ident else None,
                    "model": model,
                    "effort": effort,
                    "sandbox": args.sandbox,
                    "approval": args.approval,
                    "codex_cmd": plan["codex_cmd"],
                    "launch_cmd": plan["launch_cmd_display"],
                    "cache_file": str(cache_path),
                    "elapsed_sec": elapsed,
                    "cleanup_status": cleanup_status,
                    "note": detail,
                },
                args.json,
            )
            return 0

        if plan["mode"] == MODE_SANDBOX_BACKGROUND and managed_ident:
            terminate_verified_pid(
                managed_ident["pid"], managed_ident["pid_name"], managed_ident["pid_created_at"]
            )
        hint = (
            f"Resumed Codex session did not become ready within {args.timeout:g}s ({detail}). "
            + (
                f"In SANDBOX_BACKGROUND mode, check whether '{resolved_cwd}' is trusted in "
                "~/.codex/config.toml ([projects.'<cwd>'] trust_level = \"trusted\")."
                if plan["mode"] == MODE_SANDBOX_BACKGROUND
                else "Check the opened Codex window for directory trust or login prompts."
            )
        )
        emit_result(
            {
                "status": "RESUME_TIMEOUT",
                "thread_id": resume_uuid,
                "cwd": resolved_cwd,
                "platform": plan["platform"],
                "desktop": plan.get("desktop", ""),
                "mode": plan["mode"],
                "launcher": plan["launcher"],
                "model": model,
                "effort": effort,
                "sandbox": args.sandbox,
                "approval": args.approval,
                "codex_cmd": plan["codex_cmd"],
                "launch_cmd": plan["launch_cmd_display"],
                "cache_file": str(cache_path),
                "elapsed_sec": elapsed,
                "hint": hint,
            },
            args.json,
        )
        return 124

    # Branch B: New session launch -> poll find_codex_thread.py --refresh --since <launch_ts>
    find_script = Path(__file__).resolve().parent / "find_codex_thread.py"
    if not safe_is_file(find_script):
        print(f"ERROR: Companion script not found: {find_script}", file=sys.stderr)
        return 1

    thread_id, poll_data = poll_new_thread_uuid(
        find_script=find_script,
        resolved_cwd=resolved_cwd,
        since_ts=launch_ts,
        timeout=args.timeout,
        interval=args.interval,
        sessions_dir=sessions_dir_arg,
        cache_file=str(cache_path),
        launcher_proc=launcher_proc if plan["mode"] == MODE_SANDBOX_BACKGROUND else None,
    )
    elapsed = round(time.monotonic() - start_mono, 1)

    if thread_id:
        codex_ident = (
            find_descendant_by_name(managed_ident["pid"], {"codex.exe"})
            if (plan["mode"] == MODE_SANDBOX_BACKGROUND and managed_ident)
            else find_recent_codex_process(launch_ft)
        )
        stored_ident = managed_ident if plan["mode"] == MODE_SANDBOX_BACKGROUND else (codex_ident or managed_ident)
        update_cache_launch_metadata(
            cache_path=cache_path,
            thread_id=thread_id,
            cwd=resolved_cwd,
            model=model,
            effort=effort,
            sandbox=args.sandbox,
            approval=args.approval,
            mode=plan["mode"],
            desktop=plan.get("desktop", ""),
            proc_ident=stored_ident,
        )
        emit_result(
            {
                "status": "STARTED_AND_BOUND",
                "thread_id": thread_id,
                "cwd": resolved_cwd,
                "platform": plan["platform"],
                "desktop": plan.get("desktop", ""),
                "mode": plan["mode"],
                "launcher": plan["launcher"],
                "pid": stored_ident["pid"] if stored_ident else None,
                "pid_name": stored_ident["pid_name"] if stored_ident else "",
                "pid_created_at": stored_ident["pid_created_at"] if stored_ident else "",
                "codex_pid": codex_ident["pid"] if codex_ident else None,
                "model": model,
                "effort": effort,
                "sandbox": args.sandbox,
                "approval": args.approval,
                "codex_cmd": plan["codex_cmd"],
                "launch_cmd": plan["launch_cmd_display"],
                "cache_file": str(cache_path),
                "elapsed_sec": elapsed,
                "cleanup_status": cleanup_status,
            },
            args.json,
        )
        return 0

    if plan["mode"] == MODE_SANDBOX_BACKGROUND:
        early = isinstance(poll_data, dict) and poll_data.get("early_exit")
        if managed_ident and not early:
            # Record PID so user or --stop can clean up if stuck on trust prompt
            update_cache_launch_metadata(
                cache_path=cache_path,
                thread_id="",
                cwd=resolved_cwd,
                model=model,
                effort=effort,
                sandbox=args.sandbox,
                approval=args.approval,
                mode=plan["mode"],
                desktop=plan.get("desktop", ""),
                proc_ident=managed_ident,
            )
        hint = (
            f"No new Codex session log appeared within {args.timeout:g}s in SANDBOX_BACKGROUND mode "
            f"(desktop={plan.get('desktop', '')}). "
            "Because this isolated desktop has no visible window, Codex may be paused waiting on a "
            "first-time directory trust prompt. Add the project to ~/.codex/config.toml "
            f"([projects.'{resolved_cwd}'] with trust_level = \"trusted\"), run "
            f'python "{Path(__file__).resolve()}" --cwd "{resolved_cwd}" --stop to clean up any stuck process, '
            "and relaunch."
        )
    else:
        hint = (
            f"No new Codex session log appeared within {args.timeout:g}s. "
            "Check the newly opened Codex window to see if it is paused at the first-time "
            "directory trust confirmation prompt (or login prompt). After confirming trust "
            "and seeing the initial reply, rerun: "
            f'python "{find_script}" --cwd "{resolved_cwd}" --refresh'
        )

    emit_result(
        {
            "status": "START_TIMEOUT",
            "cwd": resolved_cwd,
            "platform": plan["platform"],
            "desktop": plan.get("desktop", ""),
            "mode": plan["mode"],
            "launcher": plan["launcher"],
            "pid": managed_ident["pid"] if managed_ident else None,
            "pid_name": managed_ident["pid_name"] if managed_ident else "",
            "pid_created_at": managed_ident["pid_created_at"] if managed_ident else "",
            "model": model,
            "effort": effort,
            "sandbox": args.sandbox,
            "approval": args.approval,
            "codex_cmd": plan["codex_cmd"],
            "launch_cmd": plan["launch_cmd_display"],
            "cache_file": str(cache_path),
            "elapsed_sec": elapsed,
            "hint": hint,
        },
        args.json,
    )
    return 124


if __name__ == "__main__":
    raise SystemExit(main())
