"""Windows primitives for owned proxies and fingerprinted legacy processes."""

from __future__ import annotations

import ctypes as ct
import datetime as dt
import os
import subprocess
from ctypes import wintypes as wt
from typing import Any, Dict

_CREATE_SUSPENDED = 0x00000004
_KILL_ON_JOB_CLOSE = 0x00002000
_QUERY = 0x1000
_TERMINATE = 0x0001
_SYNCHRONIZE = 0x00100000
_EXITED = 0
_WAIT_TIMEOUT = 258
_FILETIME_EPOCH = 11644473600 * 10_000_000


def _function(kernel: Any, name: str, restype: Any, *argtypes: Any) -> Any:
    function = getattr(kernel, name)
    function.restype = restype
    function.argtypes = list(argtypes)
    return function


class _JobLimits(ct.Structure):
    _fields_ = [
        ("process_time", ct.c_longlong), ("job_time", ct.c_longlong),
        ("flags", wt.DWORD), ("min_working_set", ct.c_size_t),
        ("max_working_set", ct.c_size_t), ("process_limit", wt.DWORD),
        ("affinity", ct.c_size_t), ("priority", wt.DWORD), ("scheduling", wt.DWORD),
    ]


class _ExtendedJobLimits(ct.Structure):
    _fields_ = [
        ("basic", _JobLimits), ("io_counters", ct.c_ulonglong * 6),
        ("process_memory", ct.c_size_t), ("job_memory", ct.c_size_t),
        ("peak_process_memory", ct.c_size_t), ("peak_job_memory", ct.c_size_t),
    ]


class _ThreadEntry(ct.Structure):
    _fields_ = [
        ("size", wt.DWORD), ("usage", wt.DWORD), ("thread_id", wt.DWORD),
        ("owner_pid", wt.DWORD), ("base_priority", wt.LONG),
        ("delta_priority", wt.LONG), ("flags", wt.DWORD),
    ]


def _resume_primary_thread(kernel: Any, pid: int, close_handle: Any) -> None:
    snapshot = _function(kernel, "CreateToolhelp32Snapshot", wt.HANDLE, wt.DWORD, wt.DWORD)(4, 0)
    if not snapshot or snapshot == ct.c_void_p(-1).value:
        raise ct.WinError(ct.get_last_error())
    try:
        first = _function(kernel, "Thread32First", wt.BOOL, wt.HANDLE, ct.POINTER(_ThreadEntry))
        next_thread = _function(kernel, "Thread32Next", wt.BOOL, wt.HANDLE, ct.POINTER(_ThreadEntry))
        entry = _ThreadEntry(size=ct.sizeof(_ThreadEntry))
        present = first(snapshot, ct.byref(entry))
        while present:
            if entry.owner_pid == pid:
                open_thread = _function(kernel, "OpenThread", wt.HANDLE, wt.DWORD, wt.BOOL, wt.DWORD)
                thread = open_thread(2, False, entry.thread_id)
                if not thread:
                    raise ct.WinError(ct.get_last_error())
                try:
                    resume_thread = _function(kernel, "ResumeThread", wt.DWORD, wt.HANDLE)
                    if resume_thread(thread) == 0xFFFFFFFF:
                        raise ct.WinError(ct.get_last_error())
                    return
                finally:
                    close_handle(thread)
            entry.size = ct.sizeof(_ThreadEntry)
            present = next_thread(snapshot, ct.byref(entry))
        raise OSError("Could not find the suspended proxy's primary thread.")
    finally:
        close_handle(snapshot)


def start_in_job(command: str, *, creationflags: int, **kwargs: Any) -> subprocess.Popen:
    kernel = ct.WinDLL("kernel32", use_last_error=True)
    close_handle = _function(kernel, "CloseHandle", wt.BOOL, wt.HANDLE)
    create_job = _function(kernel, "CreateJobObjectW", wt.HANDLE, ct.c_void_p, wt.LPCWSTR)
    job = create_job(None, None)
    if not job:
        raise ct.WinError(ct.get_last_error())
    process = None
    try:
        limits = _ExtendedJobLimits()
        limits.basic.flags = _KILL_ON_JOB_CLOSE
        configure_job = _function(kernel, "SetInformationJobObject", wt.BOOL,
                                  wt.HANDLE, ct.c_int, ct.c_void_p, wt.DWORD)
        if not configure_job(job, 9, ct.byref(limits), ct.sizeof(limits)):
            raise ct.WinError(ct.get_last_error())
        process = subprocess.Popen(command, creationflags=creationflags | _CREATE_SUSPENDED, **kwargs)
        assign = _function(kernel, "AssignProcessToJobObject", wt.BOOL, wt.HANDLE, wt.HANDLE)
        if not assign(job, int(process._handle)):
            raise ct.WinError(ct.get_last_error())
        _resume_primary_thread(kernel, process.pid, close_handle)
    except BaseException:
        try:
            if process is not None:
                process.terminate()
        finally:
            close_handle(job)
        if process is not None:
            try:
                process.wait(timeout=1)
            finally:
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
        raise

    def close_job() -> None:
        nonlocal job
        if job:
            close_handle(job)
            job = None

    process.close_job = close_job
    return process


def _creation_time(filetime: wt.FILETIME) -> str:
    ticks = (filetime.dwHighDateTime << 32) | filetime.dwLowDateTime
    seconds, fraction = divmod(ticks - _FILETIME_EPOCH, 10_000_000)
    timestamp = dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc)
    return f"{timestamp:%Y-%m-%dT%H:%M:%S}.{fraction:07d}Z"


def legacy_process_state(record: Dict[str, Any], *, terminate: bool = False, timeout: float = 0) -> str:
    """Validate and, if requested, terminate using one Windows process handle."""
    allowed_names = {"conhost.exe", "powershell.exe", "codex.exe"}
    name = str(record.get("pid_name") or "").lower()
    creation = record.get("pid_created_at")
    pid = record.get("pid")
    if (record.get("launched_by") != "start_codex_window.py" or type(pid) is not int
            or not 0 < pid <= 0xFFFFFFFF or name not in allowed_names or not creation):
        return "fingerprint_mismatch"
    if os.name != "nt":
        raise OSError("Legacy background processes can only be verified on Windows.")

    kernel = ct.WinDLL("kernel32", use_last_error=True)
    open_process = _function(kernel, "OpenProcess", wt.HANDLE, wt.DWORD, wt.BOOL, wt.DWORD)
    wait = _function(kernel, "WaitForSingleObject", wt.DWORD, wt.HANDLE, wt.DWORD)
    access = _QUERY | _SYNCHRONIZE | (_TERMINATE if terminate else 0)
    handle = open_process(access, False, pid)
    if not handle:
        error = ct.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER, no such process ID
            return "not_running"
        raise ct.WinError(error)
    try:
        state = wait(handle, 0)
        if state == _EXITED:
            return "not_running"
        if state != _WAIT_TIMEOUT:
            raise ct.WinError(ct.get_last_error())

        times = [wt.FILETIME() for _ in range(4)]
        query_times = _function(kernel, "GetProcessTimes", wt.BOOL, wt.HANDLE,
                                *([ct.POINTER(wt.FILETIME)] * 4))
        if not query_times(handle, *(ct.byref(item) for item in times)):
            raise ct.WinError(ct.get_last_error())
        executable = ct.create_unicode_buffer(32768)
        size = wt.DWORD(len(executable))
        query_name = _function(kernel, "QueryFullProcessImageNameW", wt.BOOL,
                               wt.HANDLE, wt.DWORD, wt.LPWSTR, ct.POINTER(wt.DWORD))
        if not query_name(handle, 0, executable, ct.byref(size)):
            raise ct.WinError(ct.get_last_error())
        if os.path.basename(executable.value).lower() != name or _creation_time(times[0]) != creation:
            return "fingerprint_mismatch"
        if not terminate:
            return "alive"

        terminate_process = _function(kernel, "TerminateProcess", wt.BOOL, wt.HANDLE, wt.UINT)
        if not terminate_process(handle, 1):
            if wait(handle, 0) == _EXITED:
                return "not_running"
            raise ct.WinError(ct.get_last_error())
        if wait(handle, min(0xFFFFFFFE, int(timeout * 1000))) != _EXITED:
            raise TimeoutError("The verified legacy process did not exit before the deadline.")
        return "terminated"
    finally:
        _function(kernel, "CloseHandle", wt.BOOL, wt.HANDLE)(handle)
