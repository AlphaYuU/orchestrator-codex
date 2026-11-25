"""Local path, locking, session identity, and CLI helpers for the orchestrator."""

from __future__ import annotations

import contextlib
import errno
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Sequence, Tuple, Union

MODE_VISIBLE_WINDOW = "VISIBLE_WINDOW"
MODE_DAEMON_MANAGED = "DAEMON_MANAGED"
MODE_SANDBOX_BACKGROUND = "SANDBOX_BACKGROUND"
REPORT_STATUSES = {"READY_FOR_REVIEW", "BLOCKED_NEED_CLARIFICATION"}
BOOTSTRAP_PREFIX = "[orchestrator-bootstrap:"


class Deadline:
    def __init__(self, timeout: float) -> None:
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("Timeout must be a finite, non-negative number.")
        self.end = time.monotonic() + timeout

    def remaining(self) -> float:
        return max(0.0, self.end - time.monotonic())

    def require(self) -> float:
        remaining = self.remaining()
        if not remaining:
            raise TimeoutError("The operation exceeded its time limit.")
        return remaining


def configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except OSError:
                pass


def canonical_uuid(value: Any) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid thread UUID: {value!r}") from exc


def normalize_path(value: Union[str, Path]) -> str:
    path = str(value)
    if os.name == "nt":
        if path.upper().startswith(("\\\\?\\UNC\\", "//?/UNC/")):
            path = "\\\\" + path[8:]
        elif path.startswith(("\\\\?\\", "//?/")):
            path = path[4:]
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def same_project(value: Any, root: Path) -> bool:
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        return False
    try:
        return normalize_path(value) == normalize_path(root)
    except (OSError, RuntimeError, ValueError):
        return False


def resolve_project(value: str) -> Path:
    root = Path(value).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"Project directory does not exist: {root}")
    return root


def collab_directory(root: Path) -> Path:
    directory = (root / ".agent-collab").resolve()
    if directory == root or root not in directory.parents:
        raise ValueError(f".agent-collab resolves outside its project directory: {directory}")
    return directory


def project_file(root: Path, name: str, override: str = "") -> Path:
    """Resolve project-relative arguments and confine the default collaboration paths."""
    raw = Path(override).expanduser() if override else Path(".agent-collab") / name
    if not raw.is_absolute():
        raw = root / raw
    lexical = Path(os.path.abspath(raw))
    default_directory = root / ".agent-collab"
    directory = collab_directory(root)
    if default_directory in lexical.parents or directory in lexical.parents:
        resolved = lexical.resolve()
        if directory not in resolved.parents:
            raise ValueError(f"Collaboration file resolves outside .agent-collab: {lexical}")
        return resolved
    return lexical.resolve()


def codex_home(override: str = "") -> Path:
    return Path(override or os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()


def local_path(value: str, default: Path) -> Path:
    return Path(value).expanduser().resolve() if value else default


def read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def atomic_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp",
            encoding="utf-8", newline="\n", delete=False,
        ) as target:
            temporary = Path(target.name)
            json.dump(data, target, ensure_ascii=False, indent=2)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _try_lock(file: Any, shared: bool = False) -> bool:
    try:
        if os.name == "nt":
            import msvcrt

            file.seek(0)
            mode = msvcrt.LK_NBRLCK if shared else msvcrt.LK_NBLCK
            msvcrt.locking(file.fileno(), mode, 1)
        else:
            import fcntl

            kind = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
            fcntl.flock(file.fileno(), kind | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
            return False
        raise
    return True


@contextlib.contextmanager
def project_lock(root: Path, name: str, deadline: Deadline) -> Iterator[None]:
    path = project_file(root, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep the file in place. Closing its handle releases the OS lock even on crash.
    with path.open("a+b", buffering=0) as lock:
        while not _try_lock(lock):
            time.sleep(min(0.05, deadline.require()))
        yield


def has_active_writer(home: Path, thread_id: str) -> bool:
    """Probe Codex's local rollout-writer lock without creating or modifying it."""
    directory = home / "thread-writer-locks"
    path = directory / f"{canonical_uuid(thread_id)}.lock"
    if not path.is_file():
        return False
    try:
        with (directory / ".coordination.lock").open("rb", buffering=0) as coordination:
            # New upstream writers wait for coordination before taking a thread lock.
            if not _try_lock(coordination):
                raise TimeoutError("Codex rollout writer coordination is busy.")
            with path.open("rb", buffering=0) as writer:
                return not _try_lock(writer, shared=True)
    except FileNotFoundError:
        return False


class CodexCLI:
    """A single resolved executable and Codex home for all child invocations."""

    def __init__(self, path: Path, home: Path) -> None:
        self.path = path
        self.home = home
        self.uses_cmd = os.name == "nt" and path.suffix.lower() in (".bat", ".cmd")

    @classmethod
    def resolve(cls, home: Path) -> "CodexCLI":
        path = shutil.which("codex")
        if not path:
            raise FileNotFoundError("Codex CLI was not found on PATH.")
        return cls(Path(path).resolve(strict=True), home)

    def windows_command(self, args: Sequence[str]) -> Tuple[str, str, Dict[str, str]]:
        env = {"CODEX_HOME": str(self.home)}
        if not self.uses_cmd:
            return str(self.path), subprocess.list2cmdline(args), env

        cmd_exe = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe"
        # Expand each argument once from a private environment variable. With delayed
        # expansion off, percent signs, bangs, spaces, and cmd metacharacters stay data.
        references = []
        for index, argument in enumerate([str(self.path), *args]):
            if any(character in argument for character in ('"', "\0", "\n", "\r")):
                raise ValueError("A Windows Codex batch shim cannot accept quotes or line breaks in arguments.")
            key = f"ORCHESTRATOR_CODEX_ARG_{index}"
            env[key] = argument
            references.append(f'"%{key}%"')
        return str(cmd_exe), '/d /v:off /s /c "' + " ".join(references) + '"', env

    def invocation(self, args: Sequence[str]) -> Tuple[Union[Sequence[str], str], Dict[str, str]]:
        env = os.environ.copy()
        env["CODEX_HOME"] = str(self.home)
        if not self.uses_cmd:
            return [str(self.path), *args], env
        application, parameters, updates = self.windows_command(args)
        env.update(updates)
        return subprocess.list2cmdline([application]) + " " + parameters, env


def task_marker(task_id: str, round_number: int) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", task_id):
        raise ValueError("--task-id must contain 1–96 letters, digits, dots, underscores or hyphens.")
    if round_number < 1:
        raise ValueError("--round must be a positive integer.")
    return f"[orchestrator-task:{task_id} round:{round_number}]"


def first_line(message: str) -> str:
    return message.partition("\n")[0].strip()


def message_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        block["text"] for block in content
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    )


def user_message(record: Dict[str, Any]) -> str:
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return ""
    if record.get("type") == "event_msg":
        if payload.get("type") == "user_message" and isinstance(payload.get("message"), str):
            return payload["message"]
        item = payload.get("item")
        if payload.get("type") == "item_completed" and isinstance(item, dict) and item.get("type") == "UserMessage":
            return message_content(item.get("content"))
    if record.get("type") == "response_item" and payload.get("type") == "message" and payload.get("role") == "user":
        return message_content(payload.get("content"))
    return ""
