#!/usr/bin/env python3
"""Bounded WebSocket and JSON-RPC client for a private Codex app-server proxy."""

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
from typing import Any, Callable, Dict, List, Optional, Tuple

from codex_session import CodexCLI, Deadline, canonical_uuid, same_project


_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_CONT, _TEXT, _BINARY, _CLOSE, _PING, _PONG = 0, 1, 2, 8, 9, 10
_MAX_FRAME = 8 * 1024 * 1024
_MAX_MESSAGE = 16 * 1024 * 1024
_MAX_HEADERS = 64 * 1024
_MAX_PAGES = 20
_CLOSE_TIMEOUT = 0.25


class DaemonRPCError(RuntimeError):
    """Communication or app-server protocol failure."""


class DaemonUnavailableError(DaemonRPCError):
    """The configured CLI could not start a durable local daemon."""


class DaemonProbeError(DaemonRPCError):
    """Daemon state could not be established."""


class ThreadOwnershipError(DaemonRPCError):
    """The given UUID could not be verified as belonging to this project."""


class WebSocketError(DaemonRPCError):
    def __init__(self, message: str, close_code: int = 1002) -> None:
        super().__init__(message)
        self.close_code = close_code


def _no_window() -> Dict[str, Any]:
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def _spawn_proxy(cli: CodexCLI, args: List[str], **pipes: Any) -> Any:
    command, env = cli.invocation(args)
    kwargs = _no_window()
    if not cli.uses_cmd:
        return subprocess.Popen(command, env=env, **pipes, **kwargs)

    # A batch shim can spawn Node and the native CLI. Keep the private proxy's
    # entire subtree in a kill-on-close job, assigning it before any code runs.
    from codex_windows import start_in_job

    return start_in_job(command, env=env, **pipes, **kwargs)


def _terminate_proxy(proc: Any) -> None:
    try:
        if proc.poll() is None:
            proc.terminate()
    except OSError:
        pass
    finally:
        close_job = getattr(proc, "close_job", None)
        if close_job is not None:
            close_job()
    try:
        proc.wait(timeout=0.75)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=0.75)


def _parse_json_object(raw: bytes) -> Dict[str, Any]:
    text = raw.decode("utf-8", errors="replace").strip()
    # Some CLI builds print an informational warning before the JSON document.
    for part in [text, *reversed(text.splitlines())]:
        try:
            parsed = json.loads(part)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise DaemonProbeError("Daemon version returned no JSON object.")


def get_daemon_status(cli: CodexCLI, deadline: Deadline) -> Dict[str, Any]:
    proc = None
    try:
        timeout = min(5.0, deadline.require())
        proc = _spawn_proxy(cli, ["app-server", "daemon", "version"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        stdout, stderr = proc.communicate(timeout=min(timeout, deadline.require()))
        if proc.returncode:
            message = stderr.decode("utf-8", errors="replace").strip()
            if "unrecognized subcommand" in message.lower():
                raise DaemonUnavailableError("The resolved Codex CLI lacks app-server daemon support.")
            raise DaemonProbeError(f"Daemon version failed (exit {proc.returncode}): {message}")
        data = _parse_json_object(stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise DaemonProbeError(f"Daemon version probe failed: {exc}") from exc
    finally:
        if proc is not None:
            _terminate_proxy(proc)
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()

    status = data.get("status")
    if status not in ("running", "notRunning", "not_running", "stopped"):
        raise DaemonProbeError(f"Unrecognized daemon version status: {status!r}")
    return {
        "running": status == "running",
        "status": status,
        "socket_path": data.get("socketPath") or "",
        "app_server_version": data.get("appServerVersion") or "",
    }


def ensure_daemon_running(cli: CodexCLI, deadline: Deadline) -> Dict[str, Any]:
    status = get_daemon_status(cli, deadline)
    if status["running"]:
        return status
    try:
        command, env = cli.invocation(["app-server", "daemon", "start"])
        proc = subprocess.run(
            command, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=deadline.require(), **_no_window(),
        )
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise DaemonUnavailableError(f"Daemon cold-start failed: {exc}") from exc

    while deadline.remaining():
        status = get_daemon_status(cli, deadline)
        if status["running"]:
            return status
        if proc.returncode:
            break
        time.sleep(min(0.2, deadline.remaining()))
    raise DaemonUnavailableError(
        "The daemon did not remain available after cold start. "
        "Start it from the main desktop and retry."
    )


def build_ws_frame(opcode: int, payload: bytes, mask_key: Optional[bytes] = None) -> bytes:
    if opcode not in (_TEXT, _BINARY, _CLOSE, _PING, _PONG):
        raise ValueError("Unsupported outgoing WebSocket opcode.")
    if len(payload) > (_MAX_FRAME if opcode < _CLOSE else 125):
        raise ValueError("Outgoing WebSocket frame is too large.")
    key = os.urandom(4) if mask_key is None else mask_key
    if len(key) != 4:
        raise ValueError("WebSocket mask key must be exactly 4 bytes.")
    length = len(payload)
    if length < 126:
        size = bytes([0x80 | length])
    elif length < 65536:
        size = b"\xfe" + struct.pack("!H", length)
    else:
        size = b"\xff" + struct.pack("!Q", length)
    return bytes([0x80 | opcode]) + size + key + bytes(
        value ^ key[index & 3] for index, value in enumerate(payload)
    )


def compute_ws_accept(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()).decode("ascii")


def _validate_upgrade(raw: bytes, expected_accept: str) -> None:
    lines = raw.decode("iso-8859-1").split("\r\n")
    status = lines[0].split()
    if status[:2] != ["HTTP/1.1", "101"]:
        raise DaemonRPCError(f"WebSocket upgrade rejected: {lines[0]!r}")
    headers: Dict[str, List[str]] = {}
    for line in lines[1:]:
        if not line:
            break
        name, separator, value = line.partition(":")
        if not separator or not name or name != name.strip():
            raise DaemonRPCError("Malformed WebSocket upgrade header.")
        headers.setdefault(name.lower(), []).append(value.strip())

    def tokens(name: str) -> List[str]:
        return [token.strip().lower() for value in headers.get(name, []) for token in value.split(",")]

    if (headers.get("sec-websocket-accept") != [expected_accept]
            or "websocket" not in tokens("upgrade") or "upgrade" not in tokens("connection")
            or "sec-websocket-extensions" in headers or "sec-websocket-protocol" in headers):
        raise DaemonRPCError("Invalid or incomplete WebSocket upgrade headers.")


def _validate_close(payload: bytes) -> None:
    if len(payload) == 1:
        raise WebSocketError("Malformed WebSocket Close payload.")
    if not payload:
        return
    code = int.from_bytes(payload[:2], "big")
    if code not in (1000, 1001, 1002, 1003, 1007, 1008, 1009, 1010, 1011, 1012, 1013, 1014) and not 3000 <= code <= 4999:
        raise WebSocketError(f"Invalid WebSocket Close code: {code}")
    try:
        payload[2:].decode("utf-8")
    except UnicodeError as exc:
        raise WebSocketError("WebSocket Close reason is not UTF-8.", close_code=1007) from exc


class CodexDaemonClient:
    """One connection, one outstanding RPC, and one shared operation deadline."""

    def __init__(self, cli: CodexCLI, deadline: Deadline, socket_path: str = "") -> None:
        self.cli = cli
        self.deadline = deadline
        self.socket_path = socket_path
        self._proc: Any = None
        self._reader: Optional[threading.Thread] = None
        self._workers: List[threading.Thread] = []
        self._state_lock = threading.Lock()
        self._call_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._shutdown_lock = threading.Lock()
        self._peer_closed = threading.Event()
        self._pending: Optional[Tuple[int, queue.Queue]] = None
        self._terminal_error: Optional[DaemonRPCError] = None
        self._req_id = 0
        self._upgraded = False
        self._closing = False
        self._sent_close = False

    def __enter__(self) -> "CodexDaemonClient":
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close(graceful=exc_type is None)

    def _io(self, action: Callable[[], Any], timeout: float, description: str) -> Any:
        if timeout <= 0:
            raise DaemonRPCError(f"{description} timed out.")
        result: queue.Queue = queue.Queue(maxsize=1)

        def run() -> None:
            try:
                result.put((True, action()))
            except Exception as exc:
                result.put((False, exc))

        worker = threading.Thread(target=run, daemon=True)
        with self._state_lock:
            if self._proc is None:
                raise DaemonRPCError("The daemon proxy connection is closed.")
            self._workers = [previous for previous in self._workers if previous.is_alive()]
            self._workers.append(worker)
            worker.start()
        try:
            ok, item = result.get(timeout=timeout)
        except queue.Empty as exc:
            raise DaemonRPCError(f"{description} timed out.") from exc
        if not ok:
            raise DaemonRPCError(f"{description} failed: {item}") from item
        return item

    def _write_all_with_timeout(self, data: bytes, timeout: float) -> None:
        proc = self._proc

        def write() -> None:
            with self._write_lock:
                pending = memoryview(data)
                while pending:
                    count = proc.stdin.write(pending)
                    if not count:
                        raise EOFError("Proxy stdin was closed.")
                    pending = pending[count:]
                proc.stdin.flush()

        self._io(write, timeout, "Writing to app-server proxy")

    def _read_exact(self, size: int) -> bytes:
        data = bytearray()
        proc = self._proc
        if proc is None:
            raise EOFError("Proxy stdout is closed.")
        while len(data) < size:
            chunk = proc.stdout.read(size - len(data))
            if not chunk:
                raise EOFError("Unexpected EOF from app-server proxy.")
            data.extend(chunk)
        return bytes(data)

    def _read_headers(self) -> bytes:
        raw = bytearray()
        while not raw.endswith(b"\r\n\r\n"):
            if len(raw) >= _MAX_HEADERS:
                raise DaemonRPCError("WebSocket upgrade headers exceeded 64 KiB.")
            raw.extend(self._read_exact(1))
        return bytes(raw)

    def connect(self) -> None:
        if self._proc is not None or self._closing:
            raise DaemonRPCError("A daemon client is single use.")
        args = ["app-server", "proxy"]
        if self.socket_path:
            args.extend(["--sock", self.socket_path])
        try:
            self.deadline.require()
            self._proc = _spawn_proxy(
                self.cli, args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, bufsize=0,
            )
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            request = (
                "GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode("ascii")
            self._write_all_with_timeout(request, self.deadline.remaining())
            raw = self._io(self._read_headers, self.deadline.remaining(), "WebSocket upgrade")
            _validate_upgrade(raw, compute_ws_accept(key))
            self._upgraded = True
            self._reader = threading.Thread(target=self._reader_loop, daemon=True)
            self._reader.start()
            self.call("initialize", {"clientInfo": {
                "name": "orchestrator-codex", "title": "Orchestrator Codex", "version": "1.0.0",
            }})
            self._send_json({"method": "initialized"}, self.deadline.remaining())
        except BaseException as exc:
            self.close(graceful=False)
            if isinstance(exc, (OSError, ValueError)):
                raise DaemonRPCError(f"Could not initialize app-server proxy: {exc}") from exc
            raise

    def _accept_message(self, message: Dict[str, Any]) -> None:
        identifier = message.get("id")
        if "method" in message:
            if type(identifier) in (int, str) and not self._closing:
                self._send_json({"id": identifier, "error": {
                    "code": -32601, "message": "This control client does not handle server requests.",
                }}, self.deadline.remaining())
            return
        if type(identifier) is not int or ("result" in message) == ("error" in message):
            return
        with self._state_lock:
            if self._pending is not None and self._pending[0] == identifier:
                inbox = self._pending[1]
                if inbox.empty():
                    inbox.put_nowait(message)

    def _signal_error(self, error: DaemonRPCError) -> None:
        with self._state_lock:
            if self._terminal_error is None:
                self._terminal_error = error
            if self._pending is not None and self._pending[1].empty():
                self._pending[1].put_nowait(error)

    def _reader_loop(self) -> None:
        fragment_opcode = None
        fragments = bytearray()
        try:
            while True:
                first, second = self._read_exact(2)
                fin, opcode, length = bool(first & 0x80), first & 0x0F, second & 0x7F
                if first & 0x70 or second & 0x80 or opcode not in (_CONT, _TEXT, _BINARY, _CLOSE, _PING, _PONG):
                    raise WebSocketError("Invalid WebSocket RSV, mask or opcode from server.")
                if opcode >= _CLOSE and (not fin or length > 125):
                    raise WebSocketError("Invalid WebSocket control frame.")
                if length == 126:
                    length = struct.unpack("!H", self._read_exact(2))[0]
                    if length < 126:
                        raise WebSocketError("Non-minimal WebSocket frame length.")
                elif length == 127:
                    length = struct.unpack("!Q", self._read_exact(8))[0]
                    if length & (1 << 63) or length < 65536:
                        raise WebSocketError("Invalid 64-bit WebSocket frame length.")
                if opcode < _CLOSE and (opcode == _CONT) != (fragment_opcode is not None):
                    raise WebSocketError("Invalid WebSocket continuation sequence.")
                if length > _MAX_FRAME or (opcode < _CLOSE and len(fragments) + length > _MAX_MESSAGE):
                    raise WebSocketError("WebSocket frame or message exceeded its size limit.", close_code=1009)
                payload = self._read_exact(length)
                if opcode == _CLOSE:
                    _validate_close(payload)
                    self._send_close(payload)
                    self._peer_closed.set()
                    self._signal_error(DaemonRPCError("WebSocket closed by app-server proxy."))
                    return
                if opcode == _PING:
                    if not self._closing:
                        self._write_all_with_timeout(build_ws_frame(_PONG, payload), self.deadline.remaining())
                    continue
                if opcode == _PONG:
                    continue

                if opcode != _CONT:
                    fragment_opcode = opcode
                fragments.extend(payload)
                if fin:
                    if fragment_opcode == _TEXT and not self._closing:
                        try:
                            message = json.loads(fragments.decode("utf-8"))
                        except UnicodeError as exc:
                            raise WebSocketError("WebSocket text is not UTF-8.", close_code=1007) from exc
                        if isinstance(message, dict):
                            self._accept_message(message)
                    fragments.clear()
                    fragment_opcode = None
        except (EOFError, OSError, ValueError, DaemonRPCError) as exc:
            if not self._closing:
                self._signal_error(DaemonRPCError(f"Reading app-server proxy failed: {exc}"))
                if isinstance(exc, WebSocketError):
                    try:
                        self._send_close(struct.pack("!H", exc.close_code))
                    except DaemonRPCError:
                        pass
        finally:
            if not self._closing:
                self.close(graceful=False)

    def _send_json(self, message: Dict[str, Any], timeout: float) -> None:
        data = json.dumps({"jsonrpc": "2.0", **message}, ensure_ascii=False).encode("utf-8")
        try:
            frame = build_ws_frame(_TEXT, data)
        except ValueError as exc:
            raise DaemonRPCError(str(exc)) from exc
        self._write_all_with_timeout(frame, timeout)

    def _send_close(self, payload: bytes) -> None:
        with self._state_lock:
            if self._sent_close:
                return
            self._sent_close = True
        self._write_all_with_timeout(build_ws_frame(_CLOSE, payload), _CLOSE_TIMEOUT)

    def call(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not self._call_lock.acquire(timeout=self.deadline.remaining()):
            raise DaemonRPCError(f"RPC {method!r} timed out waiting for another call.")
        try:
            with self._state_lock:
                if self._terminal_error is not None:
                    raise self._terminal_error
                if self._closing:
                    raise DaemonRPCError("The daemon proxy connection is closed.")
                self._req_id += 1
                request_id = self._req_id
                inbox: queue.Queue = queue.Queue(maxsize=1)
                self._pending = (request_id, inbox)
            try:
                self._send_json({"id": request_id, "method": method, "params": params or {}},
                                self.deadline.remaining())
                response = inbox.get(timeout=self.deadline.remaining())
            except queue.Empty as exc:
                self.close(graceful=False)
                raise DaemonRPCError(f"RPC {method!r} timed out.") from exc
            except DaemonRPCError:
                self.close(graceful=False)
                raise
            if isinstance(response, DaemonRPCError):
                raise response
            if "error" in response:
                error = response["error"]
                message = error.get("message") if isinstance(error, dict) else error
                raise DaemonRPCError(f"RPC {method!r} failed: {message}")
            result = response["result"]
            return result if isinstance(result, dict) else {"value": result}
        finally:
            with self._state_lock:
                self._pending = None
            self._call_lock.release()

    def list_loaded_threads(self) -> List[str]:
        loaded: Dict[str, None] = {}
        cursors = set()
        cursor = None
        for _ in range(_MAX_PAGES):
            result = self.call("thread/loaded/list", {"cursor": cursor} if cursor else {})
            data = result.get("data")
            if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
                raise DaemonProbeError("thread/loaded/list returned an invalid page.")
            loaded.update(dict.fromkeys(data))
            cursor = result.get("nextCursor")
            if not cursor:
                return list(loaded)
            if not isinstance(cursor, str) or cursor in cursors:
                raise DaemonProbeError("thread/loaded/list returned an invalid or repeated cursor.")
            cursors.add(cursor)
        raise DaemonProbeError("thread/loaded/list exceeded its pagination limit.")

    def read_thread(self, thread_id: str, include_turns: bool = False) -> Dict[str, Any]:
        thread = self.call("thread/read", {"threadId": thread_id, "includeTurns": include_turns}).get("thread")
        try:
            if isinstance(thread, dict) and canonical_uuid(thread.get("id")) == thread_id:
                return thread
        except ValueError:
            pass
        raise ThreadOwnershipError("thread/read did not return the requested thread UUID.")

    def verify_thread(self, thread_id: str, root: Any) -> None:
        if not same_project(self.read_thread(thread_id).get("cwd"), root):
            raise ThreadOwnershipError("The daemon thread does not belong to this project.")

    def start_thread(self, cwd: str, model: str, effort: str, sandbox: str, approval: str,
                     thread_id: str = "") -> Dict[str, Any]:
        if not approval:
            raise ValueError("Daemon threads require an explicit approval policy.")
        params: Dict[str, Any] = {
            "cwd": cwd, "model": model, "config": {"model_reasoning_effort": effort},
            "sandbox": sandbox, "approvalPolicy": approval,
        }
        if thread_id:
            params.update(threadId=thread_id, excludeTurns=True)
        return self.call("thread/resume" if thread_id else "thread/start", params)

    def start_turn(self, thread_id: str, message: str) -> Dict[str, Any]:
        result = self.call("turn/start", {
            "threadId": thread_id, "input": [{"type": "text", "text": message, "text_elements": []}],
        })
        turn = result.get("turn")
        if not isinstance(turn, dict) or not isinstance(turn.get("id"), str) or not turn["id"]:
            raise DaemonRPCError("turn/start returned no turn ID.")
        if turn.get("status") not in ("inProgress", "completed"):
            raise DaemonRPCError(f"turn/start did not confirm execution (status {turn.get('status')!r}).")
        return turn

    def interrupt_active_turn(self, thread_id: str) -> str:
        thread = self.read_thread(thread_id, include_turns=True)
        status = thread.get("status")
        if not isinstance(status, dict):
            raise DaemonRPCError("thread/read did not return the thread's runtime status.")
        if status.get("type") in ("idle", "notLoaded"):
            return status["type"]
        if status.get("type") != "active":
            raise DaemonRPCError(f"Cannot confirm the active turn state: {status!r}")
        turns = thread.get("turns")
        if not isinstance(turns, list):
            raise DaemonRPCError("thread/read did not return turn history.")
        current = next((turn for turn in reversed(turns) if isinstance(turn, dict)
                        and turn.get("status") == "inProgress"), None)
        if not current or not isinstance(current.get("id"), str) or not current["id"]:
            raise DaemonRPCError("Could not identify the active turn to interrupt.")
        turn_id = current["id"]
        self.call("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
        while self.deadline.remaining():
            after = self.read_thread(thread_id, include_turns=True)
            runtime = after.get("status")
            if isinstance(runtime, dict) and runtime.get("type") == "notLoaded":
                return "notLoaded"
            if isinstance(runtime, dict) and runtime.get("type") == "idle":
                recent = after.get("turns")
                finished = next((turn for turn in reversed(recent) if isinstance(turn, dict)
                                 and turn.get("id") == turn_id), None) if isinstance(recent, list) else None
                if finished and finished.get("status") in ("interrupted", "completed", "failed"):
                    return finished["status"]
            time.sleep(min(0.1, self.deadline.remaining()))
        raise DaemonRPCError("The active turn did not finish after interruption.")

    def unsubscribe_thread(self, thread_id: str) -> str:
        status = self.call("thread/unsubscribe", {"threadId": thread_id}).get("status")
        if status not in ("unsubscribed", "notSubscribed", "notLoaded"):
            raise DaemonRPCError(f"Unrecognized thread/unsubscribe status: {status!r}")
        return status

    def close(self, graceful: bool = True) -> None:
        if not self._shutdown_lock.acquire(blocking=False):
            return
        try:
            self._closing = True
            self._signal_error(DaemonRPCError("The daemon proxy connection is closed."))
            proc = self._proc
            if proc is None:
                return
            try:
                if graceful and self._upgraded and proc.poll() is None:
                    try:
                        self._send_close(b"")
                        self._peer_closed.wait(_CLOSE_TIMEOUT)
                    except DaemonRPCError:
                        pass

                # Break blocked OS pipe I/O before touching any stream or joining a worker.
                _terminate_proxy(proc)
            finally:
                with self._state_lock:
                    self._proc = None
                    workers = [*self._workers]
                if self._reader is not None and self._reader.ident is not None:
                    workers.append(self._reader)

                def release_streams() -> None:
                    caller = threading.current_thread()
                    for worker in workers:
                        if worker is not caller:
                            worker.join()
                    for stream in (proc.stdin, proc.stdout, proc.stderr):
                        if stream is not None:
                            try:
                                stream.close()
                            except OSError:
                                pass

                # Even an unexpected extra peer handle cannot make caller cleanup block.
                if self._reader is threading.current_thread():
                    workers.remove(self._reader)
                reap = threading.Thread(target=release_streams, daemon=True)
                reap.start()
                reap.join(0.5)
        finally:
            self._shutdown_lock.release()


def check_daemon_thread_loaded(cli: CodexCLI, deadline: Deadline, thread_id: str, root: Any) -> bool:
    status = get_daemon_status(cli, deadline)
    if not status["running"]:
        return False
    with CodexDaemonClient(cli, deadline, status["socket_path"]) as client:
        if thread_id not in client.list_loaded_threads():
            return False
        client.verify_thread(thread_id, root)
        return True
