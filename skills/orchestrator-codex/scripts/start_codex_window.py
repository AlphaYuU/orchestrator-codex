#!/usr/bin/env python3
"""Launch a standalone Codex CLI window with explicit user-chosen model, effort, and permissions,
then auto-capture and bind its newly created session UUID into .agent-collab/session.json.

Zero third-party dependencies. Compatible with Python 3.8+ on Windows 11, macOS, and Linux.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover - Python 3.8-3.10 fallback
    tomllib = None  # type: ignore[assignment]

SANDBOX_CHOICES = ("read-only", "workspace-write", "danger-full-access")
APPROVAL_CHOICES = ("untrusted", "on-failure", "on-request", "never")
STANDARD_EFFORTS = ("low", "medium", "high", "xhigh")
KNOWN_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")


def configure_utf8_stdio() -> None:
    """Ensure UTF-8 console output on Windows (avoids GBK crashes on Unicode paths)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except OSError:
                pass


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


def safe_is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


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
    payload = {
        "status": status,
        "codex_home": catalog["codex_home"],
        "models_cache_file": catalog["models_cache_file"],
        "fetched_at": catalog["fetched_at"],
        "client_version": catalog["client_version"],
        "config_file": catalog["config_file"],
        "config_defaults": catalog["config_defaults"],
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
    if catalog["models_cache_error"]:
        payload["error"] = catalog["models_cache_error"]

    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if has_models else 1

    print(f"STATUS: {status}")
    print(f"CODEX_HOME: {catalog['codex_home']}")
    print(f"MODELS_CACHE: {catalog['models_cache_file']}")
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
) -> List[str]:
    argv = ["codex", "-m", model, "-c", f"model_reasoning_effort={effort}"]
    if sandbox:
        argv += ["-s", sandbox]
    if approval:
        argv += ["-a", approval]
    if init_prompt:
        argv.append(init_prompt)
    return argv


def build_powershell_codex_command(
    model: str,
    effort: str,
    sandbox: str = "",
    approval: str = "",
    init_prompt: str = "hi",
) -> str:
    parts = [
        "codex",
        "-m",
        ps_arg(model),
        "-c",
        ps_arg(f"model_reasoning_effort={effort}"),
    ]
    if sandbox:
        parts += ["-s", ps_arg(sandbox)]
    if approval:
        parts += ["-a", ps_arg(approval)]
    if init_prompt:
        parts.append(ps_quote(init_prompt))
    return " ".join(parts)


def build_posix_codex_command(
    model: str,
    effort: str,
    sandbox: str = "",
    approval: str = "",
    init_prompt: str = "hi",
) -> str:
    argv = build_codex_argv(model, effort, sandbox, approval, init_prompt)
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
    platform_override: str = "",
    force_no_wt: bool = False,
) -> Dict[str, Any]:
    """Build cross-platform independent terminal window launch command."""
    plat = platform_override or ("win32" if os.name == "nt" else sys.platform)

    if plat.startswith("win"):
        ps_cmd = build_powershell_codex_command(model, effort, sandbox, approval, init_prompt)
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
            "launcher": "powershell-start-process",
            "codex_cmd": ps_cmd,
            "launch_argv": argv,
            "launch_cmd_display": f'powershell -NoProfile -Command "{sp_cmd}"',
        }

    posix_codex = build_posix_codex_command(model, effort, sandbox, approval, init_prompt)

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
                "launcher": binary,
                "codex_cmd": posix_codex,
                "launch_argv": argv,
                "launch_cmd_display": " ".join(shlex.quote(x) for x in argv),
            }

    return {
        "platform": "linux",
        "launcher": "",
        "codex_cmd": posix_codex,
        "launch_argv": [],
        "launch_cmd_display": "",
    }


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


def update_cache_launch_metadata(
    cache_path: Path,
    thread_id: str,
    cwd: str,
    model: str,
    effort: str,
    sandbox: str,
    approval: str,
) -> None:
    existing: Dict[str, Any] = {}
    if cache_path.is_file():
        try:
            loaded = json.loads(cache_path.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, json.JSONDecodeError):
            existing = {}

    existing["thread_id"] = thread_id
    existing["cwd"] = existing.get("cwd") or cwd
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
    existing["launched_by"] = "start_codex_window.py"

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def poll_new_thread_uuid(
    find_script: Path,
    resolved_cwd: str,
    since_ts: float,
    timeout: float,
    interval: float,
    sessions_dir: str = "",
    cache_file: str = "",
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
        ("launcher", "LAUNCHER"),
        ("model", "MODEL"),
        ("effort", "EFFORT"),
        ("sandbox", "SANDBOX"),
        ("approval", "APPROVAL"),
        ("codex_cmd", "CODEX_CMD"),
        ("launch_cmd", "LAUNCH_CMD"),
        ("cache_file", "CACHE_FILE"),
        ("elapsed_sec", "ELAPSED_SEC"),
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
            "Probe real Codex models (--list-models) or launch a standalone Codex CLI window "
            "with explicit --model and --effort, then bind the newly created session UUID."
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
        "--cwd",
        default=os.getcwd(),
        help="Target project root directory (default: current working directory).",
    )
    parser.add_argument(
        "--model",
        default="",
        help="Required when launching: exact Codex model slug (passed as -m <model>). Never defaulted.",
    )
    parser.add_argument(
        "--effort",
        default="",
        help=(
            "Required when launching: reasoning effort, e.g. low|medium|high|xhigh|max|ultra "
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
        help="Always open a brand-new standalone Codex window and overwrite .agent-collab/session.json.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=25.0,
        help="Seconds to wait for the new Codex session UUID to appear (default: 25).",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Polling interval in seconds when waiting for the new UUID (default: 1.0).",
    )
    parser.add_argument(
        "--init-prompt",
        default="hi",
        help="Initial message sent to initialize the Codex session log (default: 'hi').",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate arguments and print the final launch command without opening a window.",
    )
    parser.add_argument(
        "--codex-home",
        default="",
        help="Override Codex home directory (default: $CODEX_HOME or ~/.codex).",
    )
    parser.add_argument(
        "--sessions-dir",
        default="",
        help="Override Codex sessions directory forwarded to find_codex_thread.py.",
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

    codex_home = resolve_codex_home(args.codex_home)
    catalog = load_models_catalog(codex_home, include_hidden=args.include_hidden)

    if args.list_models:
        return emit_models_list(catalog, args.json)

    # Hard validation: --model and --effort are strictly mandatory; never fall back to config.toml
    model = args.model.strip()
    effort = args.effort.strip().lower()
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

    if args.timeout < 0 or args.interval <= 0:
        print("ERROR: --timeout must be >= 0 and --interval must be > 0.", file=sys.stderr)
        return 2

    cwd_path = Path(args.cwd).expanduser()
    try:
        resolved_cwd = str(cwd_path.resolve())
    except OSError:
        resolved_cwd = os.path.abspath(str(cwd_path))

    if not args.dry_run and not Path(resolved_cwd).is_dir():
        print(f"ERROR: Target --cwd directory does not exist: {resolved_cwd}", file=sys.stderr)
        return 2

    target_norm_cwd = normalize_path(resolved_cwd)
    cache_path = (
        Path(args.cache_file).expanduser()
        if args.cache_file
        else Path(resolved_cwd) / ".agent-collab" / "session.json"
    )
    sessions_dir = args.sessions_dir or (str(codex_home / "sessions") if args.codex_home else "")

    plan = build_launch_plan(
        cwd=resolved_cwd,
        model=model,
        effort=effort,
        sandbox=args.sandbox,
        approval=args.approval,
        init_prompt=args.init_prompt,
    )

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
                "cwd": resolved_cwd,
                "platform": plan["platform"],
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

    if not args.force_new:
        existing = read_existing_cache(cache_path, target_norm_cwd)
        if existing:
            emit_result(
                {
                    "status": "ALREADY_BOUND",
                    "thread_id": existing["thread_id"],
                    "cwd": existing.get("cwd", resolved_cwd),
                    "model": existing.get("model", ""),
                    "effort": existing.get("reasoning_effort", ""),
                    "cache_file": str(cache_path),
                    "note": (
                        "An active Codex session is already cached for this project. "
                        "Reuse THREAD_ID directly or rerun with --force-new to open a new window and rebind."
                    ),
                },
                args.json,
            )
            return 0

    find_script = Path(__file__).resolve().parent / "find_codex_thread.py"
    if not find_script.is_file():
        print(f"ERROR: Companion script not found: {find_script}", file=sys.stderr)
        return 1

    launch_ts = time.time()
    start_mono = time.monotonic()

    try:
        popen_kwargs: Dict[str, Any] = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = (
                getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            )
        else:
            popen_kwargs["start_new_session"] = True
        subprocess.Popen(plan["launch_argv"], **popen_kwargs)
    except OSError as exc:
        print(f"ERROR: Failed to launch Codex terminal window: {exc}", file=sys.stderr)
        return 1

    thread_id, _ = poll_new_thread_uuid(
        find_script=find_script,
        resolved_cwd=resolved_cwd,
        since_ts=launch_ts,
        timeout=args.timeout,
        interval=args.interval,
        sessions_dir=sessions_dir,
        cache_file=str(cache_path),
    )
    elapsed = round(time.monotonic() - start_mono, 1)

    if thread_id:
        update_cache_launch_metadata(
            cache_path=cache_path,
            thread_id=thread_id,
            cwd=resolved_cwd,
            model=model,
            effort=effort,
            sandbox=args.sandbox,
            approval=args.approval,
        )
        emit_result(
            {
                "status": "STARTED_AND_BOUND",
                "thread_id": thread_id,
                "cwd": resolved_cwd,
                "platform": plan["platform"],
                "launcher": plan["launcher"],
                "model": model,
                "effort": effort,
                "sandbox": args.sandbox,
                "approval": args.approval,
                "codex_cmd": plan["codex_cmd"],
                "launch_cmd": plan["launch_cmd_display"],
                "cache_file": str(cache_path),
                "elapsed_sec": elapsed,
            },
            args.json,
        )
        return 0

    emit_result(
        {
            "status": "START_TIMEOUT",
            "cwd": resolved_cwd,
            "platform": plan["platform"],
            "launcher": plan["launcher"],
            "model": model,
            "effort": effort,
            "sandbox": args.sandbox,
            "approval": args.approval,
            "codex_cmd": plan["codex_cmd"],
            "launch_cmd": plan["launch_cmd_display"],
            "cache_file": str(cache_path),
            "elapsed_sec": elapsed,
            "hint": (
                f"No new Codex session log appeared within {args.timeout:g}s. "
                "Check the newly opened Codex window to see if it is paused at the first-time "
                "directory trust confirmation prompt (or login prompt). After confirming trust "
                "and seeing the initial reply, rerun: "
                f'python "{find_script}" --cwd "{resolved_cwd}" --refresh'
            ),
        },
        args.json,
    )
    return 124


if __name__ == "__main__":
    raise SystemExit(main())
