#!/usr/bin/env python3
"""Zero-dependency RFC 6455 WebSocket + JSON-RPC 2.0 client for Codex's local
managed app-server daemon (`codex app-server proxy` / `codex app-server daemon`).

Used by `start_codex_window.py` and `find_codex_thread.py` in `DAEMON_MANAGED`
mode on isolated sandbox desktops so threads survive `run_command` exits without
spawning `wt.exe` or `conhost.exe` and without any PID-killing heuristics.

Zero third-party dependencies. Compatible with Python 3.8+ on Windows, macOS, and Linux.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import struct
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

MODE_DAEMON_MANAGED = "DAEMON_MANAGED"

_WS_MAGIC_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_OP_CONT = 0x0
_OP_TEXT = 0x1
_OP_BINARY = 0x2
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA


class DaemonRPCError(RuntimeError):
    """Raised when communicating with the Codex app-server daemon fails."""


class DaemonUnavailableError(DaemonRPCError):
    """Raised when the local managed Codex daemon is not running and cannot persist."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


def _parse_json_object(raw: str) -> Optional[Dict[str, Any]]:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    # Handle leading/trailing warnings before/after JSON line
    for line in reversed(text.splitlines()):
        s = line.strip()
        if s.startswith("{") and s.endswith("}"):
            try:
                data = json.loads(s)
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                continue
    return None


def _no_window_kwargs() -> Dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def get_daemon_status(timeout: float = 5.0) -> Dict[str, Any]:
    """Query `codex app-server daemon version` in a fresh subprocess."""
    try:
        proc = subprocess.run(
            ["codex", "app-server", "daemon", "version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1.0, timeout),
            check=False,
            **_no_window_kwargs(),
        )
    except FileNotFoundError:
        return {
            "running": False,
            "status": "cli_not_found",
            "error": "codex CLI executable was not found in PATH.",
        }
    except subprocess.TimeoutExpired:
        return {
            "running": False,
            "status": "timeout",
            "error": f"`codex app-server daemon version` timed out after {timeout:g}s.",
        }
    except OSError as exc:
        return {
            "running": False,
            "status": "os_error",
            "error": str(exc),
        }

    parsed = _parse_json_object(proc.stdout)
    if parsed is None:
        err_text = (proc.stderr or proc.stdout).strip()
        unsupported = "unrecognized subcommand" in err_text.lower()
        return {
            "running": False,
            "status": "unsupported_cli" if unsupported else "not_running",
            "returncode": proc.returncode,
            "error": err_text,
        }

    status_str = str(parsed.get("status") or "").strip().lower()
    running = status_str == "running"
    return {
        "running": running,
        "status": status_str or ("running" if proc.returncode == 0 else "stopped"),
        "socket_path": str(parsed.get("socketPath") or ""),
        "cli_version": str(parsed.get("cliVersion") or ""),
        "app_server_version": str(parsed.get("appServerVersion") or ""),
        "managed_codex_version": str(parsed.get("managedCodexVersion") or ""),
        "backend": str(parsed.get("backend") or ""),
        "raw": parsed,
    }


def ensure_daemon_running(
    timeout: float = 10.0,
    settle_sec: float = 0.8,
) -> Dict[str, Any]:
    """Ensure the local managed Codex app-server daemon is running and persists.

    If the daemon is not running, attempts `codex app-server daemon start`, waits
    briefly, and verifies liveness via an independent cross-subprocess
    `codex app-server daemon version` call. If a sandbox Job Object reclaims the
    daemon immediately upon exit, raises DaemonUnavailableError with an explicit
    instruction for the user to start it once on the main desktop.
    """
    initial = get_daemon_status(timeout=min(5.0, timeout))
    if initial.get("running"):
        return {**initial, "cold_started": False}

    if initial.get("status") == "cli_not_found":
        raise DaemonUnavailableError(
            "codex CLI executable was not found in PATH.",
            hint="Install Codex CLI and ensure `codex` is available in PATH.",
        )
    if initial.get("status") == "unsupported_cli":
        raise DaemonUnavailableError(
            "Installed Codex CLI does not support `codex app-server daemon`.",
            hint="Upgrade Codex CLI (`codex update`) to a version supporting `codex app-server daemon`.",
        )

    start_err = ""
    try:
        start_proc = subprocess.run(
            ["codex", "app-server", "daemon", "start"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(2.0, timeout),
            check=False,
            **_no_window_kwargs(),
        )
        if start_proc.returncode != 0:
            start_err = (start_proc.stderr or start_proc.stdout).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        start_err = str(exc)

    if settle_sec > 0:
        time.sleep(settle_sec)

    # Independent cross-subprocess liveness check (catches sandbox Job Object reclamation)
    recheck = get_daemon_status(timeout=min(5.0, timeout))
    if recheck.get("running"):
        return {**recheck, "cold_started": True}

    detail = start_err or recheck.get("error") or recheck.get("status") or "not_running"
    raise DaemonUnavailableError(
        f"Local Codex app-server daemon is not running after cold-start attempt ({detail}).",
        hint=(
            "The sandbox environment may have reclaimed the daemon subprocess on exit (Job Object). "
            "Run `codex app-server daemon start` once in a normal main-desktop terminal so the "
            "managed daemon persists outside the sandbox, then retry."
        ),
    )


def build_ws_frame(opcode: int, payload: bytes, mask_key: Optional[bytes] = None) -> bytes:
    """Encode a single FIN=1 client-masked RFC 6455 WebSocket frame (supports 7/16/64-bit lengths)."""
    if mask_key is None:
        mask_key = os.urandom(4)
    if len(mask_key) != 4:
        raise ValueError("WebSocket mask key must be exactly 4 bytes.")

    header = bytearray([0x80 | (opcode & 0x0F)])
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", length))

    masked = bytes(b ^ mask_key[i & 3] for i, b in enumerate(payload))
    return bytes(header) + mask_key + masked


def compute_ws_accept(sec_websocket_key: str) -> str:
    digest = hashlib.sha1((sec_websocket_key + _WS_MAGIC_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


class CodexDaemonClient:
    """RFC 6455 WebSocket + JSON-RPC 2.0 client over `codex app-server proxy`.

    All reads and writes enforce strict wall-clock timeouts using background I/O
    worker threads so an unresponsive socket or large frame can never block callers.
    """

    def __init__(
        self,
        timeout: float = 15.0,
        socket_path: str = "",
        client_name: str = "orchestrator-codex",
        client_version: str = "1.0.0",
    ) -> None:
        self.timeout = max(1.0, float(timeout))
        self.socket_path = socket_path.strip()
        self.client_name = client_name
        self.client_version = client_version
        self._proc: Optional[subprocess.Popen[bytes]] = None
        self._write_lock = threading.Lock()
        self._closed = False
        self._req_id = 0
        self._initialized = False
        self._reader_thread: Optional[threading.Thread] = None
        self._incoming: "queue.Queue[Tuple[str, Any]]" = queue.Queue()

    def __enter__(self) -> "CodexDaemonClient":
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def _write_all_with_timeout(self, data: bytes, timeout: float) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or self._closed:
            raise DaemonRPCError("Daemon proxy connection is closed.")

        err_box: List[BaseException] = []

        def _do_write() -> None:
            try:
                with self._write_lock:
                    assert proc.stdin is not None
                    proc.stdin.write(data)
                    proc.stdin.flush()
            except BaseException as exc:
                err_box.append(exc)

        writer = threading.Thread(target=_do_write, daemon=True)
        writer.start()
        writer.join(timeout=timeout)
        if writer.is_alive():
            self.close()
            raise DaemonRPCError(f"Write to `codex app-server proxy` timed out after {timeout:g}s.")
        if err_box:
            raise DaemonRPCError(f"Write to `codex app-server proxy` failed: {err_box[0]}") from err_box[0]

    def _read_exact_raw(self, n: int) -> bytes:
        proc = self._proc
        if proc is None or proc.stdout is None:
            raise EOFError("Daemon proxy stdout unavailable.")
        buf = bytearray()
        while len(buf) < n:
            chunk = proc.stdout.read(n - len(buf))
            if not chunk:
                raise EOFError("Unexpected EOF from `codex app-server proxy`.")
            buf.extend(chunk)
        return bytes(buf)

    def _read_http_headers_with_timeout(self, timeout: float) -> bytes:
        box: Dict[str, Any] = {}

        def _read_hdr() -> None:
            try:
                resp = bytearray()
                while b"\r\n\r\n" not in resp:
                    if len(resp) > 65536:
                        raise DaemonRPCError("HTTP WebSocket upgrade response exceeded 64 KiB.")
                    resp.extend(self._read_exact_raw(1))
                box["data"] = bytes(resp)
            except BaseException as exc:
                box["err"] = exc

        t = threading.Thread(target=_read_hdr, daemon=True)
        t.start()
        t.join(timeout=timeout)
        if t.is_alive():
            self.close()
            raise DaemonRPCError(f"WebSocket handshake timed out after {timeout:g}s.")
        if "err" in box:
            raise DaemonRPCError(f"WebSocket handshake failed: {box['err']}") from box["err"]
        return box["data"]

    def _reader_loop(self) -> None:
        fragments: List[bytes] = []
        frag_opcode: Optional[int] = None
        try:
            while not self._closed:
                hdr = self._read_exact_raw(2)
                fin = bool(hdr[0] & 0x80)
                opcode = hdr[0] & 0x0F
                masked = bool(hdr[1] & 0x80)
                length = hdr[1] & 0x7F
                if length == 126:
                    length = struct.unpack("!H", self._read_exact_raw(2))[0]
                elif length == 127:
                    length = struct.unpack("!Q", self._read_exact_raw(8))[0]
                mask = self._read_exact_raw(4) if masked else b""
                payload = self._read_exact_raw(length) if length else b""
                if masked:
                    payload = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))

                # Control frames (RFC 6455 §5.5) may be injected between fragments
                if opcode == _OP_PING:
                    try:
                        self._write_all_with_timeout(build_ws_frame(_OP_PONG, payload), min(3.0, self.timeout))
                    except DaemonRPCError:
                        pass
                    continue
                if opcode == _OP_PONG:
                    continue
                if opcode == _OP_CLOSE:
                    self._incoming.put(("eof", "WebSocket closed by daemon"))
                    return

                if opcode in (_OP_TEXT, _OP_BINARY):
                    if frag_opcode is not None:
                        fragments.clear()
                    frag_opcode = opcode
                    fragments.append(payload)
                elif opcode == _OP_CONT:
                    if frag_opcode is None:
                        continue
                    fragments.append(payload)
                else:
                    continue

                if fin:
                    full = b"".join(fragments)
                    fragments.clear()
                    cur_op = frag_opcode
                    frag_opcode = None
                    if cur_op == _OP_TEXT:
                        try:
                            msg = json.loads(full.decode("utf-8", errors="replace"))
                            if isinstance(msg, dict):
                                self._incoming.put(("msg", msg))
                        except json.JSONDecodeError:
                            continue
        except BaseException as exc:
            if not self._closed:
                self._incoming.put(("err", exc))

    def connect(self) -> None:
        if self._proc is not None and not self._closed:
            return
        cmd = ["codex", "app-server", "proxy"]
        if self.socket_path:
            cmd += ["--sock", self.socket_path]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **_no_window_kwargs(),
            )
        except OSError as exc:
            raise DaemonRPCError(f"Failed to spawn `codex app-server proxy`: {exc}") from exc

        ws_key = base64.b64encode(os.urandom(16)).decode("ascii")
        expected_accept = compute_ws_accept(ws_key)
        handshake = (
            "GET / HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {ws_key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        self._write_all_with_timeout(handshake, self.timeout)
        raw_resp = self._read_http_headers_with_timeout(self.timeout)
        resp_text = raw_resp.decode("utf-8", errors="replace")
        status_line = resp_text.splitlines()[0] if resp_text else ""
        if " 101 " not in status_line:
            self.close()
            raise DaemonRPCError(f"WebSocket upgrade rejected by daemon: {status_line!r}")

        headers_lower = {}
        for line in resp_text.splitlines()[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers_lower[k.strip().lower()] = v.strip()
        accept_hdr = headers_lower.get("sec-websocket-accept", "")
        if accept_hdr and accept_hdr != expected_accept:
            self.close()
            raise DaemonRPCError("WebSocket Sec-WebSocket-Accept validation failed.")

        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

        self.call(
            "initialize",
            {
                "clientInfo": {
                    "name": self.client_name,
                    "title": self.client_name,
                    "version": self.client_version,
                }
            },
        )
        self.notify("initialized", {})
        self._initialized = True

    def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        payload = json.dumps(
            {"jsonrpc": "2.0", "method": method, "params": params or {}},
            ensure_ascii=False,
        ).encode("utf-8")
        self._write_all_with_timeout(build_ws_frame(_OP_TEXT, payload), self.timeout)

    def call(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        op_timeout = self.timeout if timeout is None else max(0.5, float(timeout))
        self._req_id += 1
        req_id = self._req_id
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}},
            ensure_ascii=False,
        ).encode("utf-8")
        self._write_all_with_timeout(build_ws_frame(_OP_TEXT, payload), op_timeout)

        deadline = time.monotonic() + op_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DaemonRPCError(f"RPC `{method}` timed out after {op_timeout:g}s.")
            try:
                kind, item = self._incoming.get(timeout=remaining)
            except queue.Empty:
                raise DaemonRPCError(f"RPC `{method}` timed out after {op_timeout:g}s.")
            if kind == "err":
                raise DaemonRPCError(f"Daemon connection error during `{method}`: {item}") from item
            if kind == "eof":
                raise DaemonRPCError(f"Daemon closed connection during `{method}`: {item}")
            if isinstance(item, dict) and item.get("id") == req_id:
                if "error" in item and item["error"] is not None:
                    err = item["error"]
                    msg = err.get("message") if isinstance(err, dict) else str(err)
                    raise DaemonRPCError(f"Daemon RPC `{method}` returned error: {msg}")
                result = item.get("result")
                return result if isinstance(result, dict) else {"value": result}

    def list_loaded_threads(self) -> List[str]:
        loaded: List[str] = []
        cursor: Optional[str] = None
        for _ in range(20):
            params: Dict[str, Any] = {}
            if cursor:
                params["cursor"] = cursor
            res = self.call("thread/loaded/list", params)
            batch = res.get("data") or []
            if isinstance(batch, list):
                for item in batch:
                    if isinstance(item, str) and item not in loaded:
                        loaded.append(item)
            next_cursor = res.get("nextCursor")
            if not next_cursor:
                break
            cursor = str(next_cursor)
        return loaded

    def start_thread(
        self,
        cwd: str,
        model: str,
        effort: str,
        sandbox: str = "",
        approval: str = "",
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "cwd": cwd,
            "model": model,
            "config": {"model_reasoning_effort": effort},
        }
        if sandbox:
            params["sandbox"] = sandbox
        if approval:
            params["approvalPolicy"] = approval

        res = self.call("thread/start", params)
        thread = res.get("thread") if isinstance(res.get("thread"), dict) else {}
        thread_id = str(thread.get("id") or "").strip()
        if not thread_id:
            raise DaemonRPCError("`thread/start` succeeded without returning thread.id.")
        return {
            "thread_id": thread_id,
            "session_file": str(thread.get("path") or ""),
            "model": str(res.get("model") or model),
            "reasoning_effort": str(res.get("reasoningEffort") or effort),
            "approval_policy": res.get("approvalPolicy"),
            "sandbox": res.get("sandbox"),
            "raw": res,
        }

    def resume_thread(
        self,
        thread_id: str,
        cwd: str = "",
        model: str = "",
        effort: str = "",
        sandbox: str = "",
        approval: str = "",
        exclude_turns: bool = True,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "threadId": thread_id,
            "excludeTurns": bool(exclude_turns),
        }
        if cwd:
            params["cwd"] = cwd
        if model:
            params["model"] = model
        if effort:
            params["config"] = {"model_reasoning_effort": effort}
        if sandbox:
            params["sandbox"] = sandbox
        if approval:
            params["approvalPolicy"] = approval

        res = self.call("thread/resume", params)
        thread = res.get("thread") if isinstance(res.get("thread"), dict) else {}
        resolved_id = str(thread.get("id") or thread_id).strip()
        return {
            "thread_id": resolved_id,
            "session_file": str(thread.get("path") or ""),
            "model": str(res.get("model") or model),
            "reasoning_effort": str(res.get("reasoningEffort") or effort),
            "approval_policy": res.get("approvalPolicy"),
            "sandbox": res.get("sandbox"),
            "raw": res,
        }

    def interrupt_active_turn(self, thread_id: str) -> bool:
        """Best-effort interrupt of an active turn on thread_id (no-op if idle)."""
        try:
            res = self.call("thread/read", {"threadId": thread_id, "includeTurns": True}, timeout=5.0)
        except DaemonRPCError:
            return False
        thread = res.get("thread") if isinstance(res.get("thread"), dict) else {}
        turns = thread.get("turns") if isinstance(thread.get("turns"), list) else []
        for turn in reversed(turns):
            if not isinstance(turn, dict):
                continue
            status = str(turn.get("status") or "").lower()
            turn_id = str(turn.get("id") or "").strip()
            if turn_id and status in ("inprogress", "in_progress", "running", "active", "queued"):
                try:
                    self.call("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=5.0)
                    return True
                except DaemonRPCError:
                    return False
        return False

    def unsubscribe_thread(self, thread_id: str) -> Dict[str, Any]:
        """Gracefully unsubscribe thread_id on the managed daemon (never kills daemon/processes)."""
        res = self.call("thread/unsubscribe", {"threadId": thread_id})
        status = str(res.get("status") or "")
        if status == "notSubscribed":
            # A fresh proxy connection is not subscribed yet; rejoin briefly then unsubscribe cleanly
            try:
                loaded = self.list_loaded_threads()
                if thread_id in loaded:
                    self.call("thread/resume", {"threadId": thread_id, "excludeTurns": True})
                    res = self.call("thread/unsubscribe", {"threadId": thread_id})
            except DaemonRPCError:
                pass
        return res

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                try:
                    proc.stdin.write(build_ws_frame(_OP_CLOSE, b""))
                    proc.stdin.flush()
                except OSError:
                    pass
                try:
                    proc.stdin.close()
                except OSError:
                    pass
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=2.0)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=1.0)
            except Exception:
                pass


def check_daemon_thread_loaded(
    thread_id: str,
    timeout: float = 8.0,
) -> Tuple[bool, str, Dict[str, Any]]:
    """Return (is_loaded, reason, daemon_info) for a DAEMON_MANAGED thread."""
    if not thread_id:
        return False, "missing_thread_id", {}
    info = get_daemon_status(timeout=min(5.0, timeout))
    if not info.get("running"):
        return False, "daemon_not_running", info
    try:
        with CodexDaemonClient(timeout=timeout, socket_path=info.get("socket_path", "")) as client:
            loaded = client.list_loaded_threads()
    except DaemonRPCError as exc:
        return False, f"daemon_rpc_error:{exc}", info
    if thread_id in loaded:
        return True, "loaded_in_daemon", info
    return False, "thread_not_loaded", info
