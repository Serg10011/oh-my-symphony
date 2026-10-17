"""Private Windows backend ownership; never infer a family from reusable PIDs."""

from __future__ import annotations

import ctypes
import os
import re
import sys
import uuid
from functools import lru_cache
from typing import Any

_TOKEN = re.compile(r"winjob-v1:(0|[1-9][0-9]{0,9}):([a-f0-9]{32})\Z")
_QUERY = 0x0004
_TERMINATE = 0x0008


class _Limits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _Limits),
        ("IoInfo", ctypes.c_uint64 * 6),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _Accounting(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", ctypes.c_uint32), ("TotalProcesses", ctypes.c_uint32),
        ("ActiveProcesses", ctypes.c_uint32), ("TotalTerminatedProcesses", ctypes.c_uint32),
    ]


@lru_cache(maxsize=1)
def _api() -> Any:
    if sys.platform != "win32":
        raise OSError("Windows job objects are unavailable")
    api = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    handle, dword, boolean = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int
    signatures = {
        "CreateJobObjectW": ([ctypes.c_void_p, ctypes.c_wchar_p], handle),
        "OpenJobObjectW": ([dword, boolean, ctypes.c_wchar_p], handle),
        "SetInformationJobObject": ([handle, ctypes.c_int, ctypes.c_void_p, dword], boolean),
        "QueryInformationJobObject": ([handle, ctypes.c_int, ctypes.c_void_p, dword, ctypes.c_void_p], boolean),
        "AssignProcessToJobObject": ([handle, handle], boolean),
        "TerminateJobObject": ([handle, dword], boolean),
        "GetCurrentProcess": ([], handle),
        "OpenProcess": ([dword, boolean, dword], handle),
        "GetProcessTimes": ([handle, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p], boolean),
        "ProcessIdToSessionId": ([dword, ctypes.c_void_p], boolean),
        "WaitForSingleObject": ([handle, dword], dword),
        "CloseHandle": ([handle], boolean),
        "ExitProcess": ([dword], None),
    }
    for name, (args, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes, function.restype = args, result
    return api


def _error() -> OSError:
    code = getattr(ctypes, "get_last_error")()
    return OSError(code, "Windows backend ownership API failed")


def session_id() -> int:
    session = ctypes.c_uint32()
    if not _api().ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
        raise _error()
    return session.value


def new_job_identity() -> str:
    return f"winjob-v1:{session_id()}:{uuid.uuid4().hex}"


def is_job_identity(identity: str | None) -> bool:
    # Unknown versions and malformed tokens must not fall through to PID signaling.
    return isinstance(identity, str) and identity.startswith("winjob-")


def _job_name(identity: str) -> str:
    match = _TOKEN.fullmatch(identity)
    if match is None or int(match[1]) != session_id():
        raise ValueError("invalid or different-session Windows job identity")
    return f"Local\\symphony-backend-{match[1]}-{match[2]}"


def create_own_job(identity: str) -> int:
    api = _api()
    getattr(ctypes, "set_last_error")(0)
    job = api.CreateJobObjectW(None, _job_name(identity))
    if not job:
        raise _error()
    if getattr(ctypes, "get_last_error")() == 183:
        api.CloseHandle(job)
        raise OSError("Windows backend job identity already exists")
    limits = _ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE; no breakaway.
    if not api.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        api.CloseHandle(job)
        raise _error()
    if not api.AssignProcessToJobObject(job, api.GetCurrentProcess()):
        api.CloseHandle(job)
        raise _error()
    return int(job)


def job_exists(identity: str) -> bool | None:
    try:
        api = _api()
        job = api.OpenJobObjectW(_QUERY, False, _job_name(identity))
        if not job:
            return False if getattr(ctypes, "get_last_error")() == 2 else None
        try:
            info = _Accounting()
            if not api.QueryInformationJobObject(job, 1, ctypes.byref(info), ctypes.sizeof(info), None):
                return None
            return info.ActiveProcesses > 0
        finally:
            api.CloseHandle(job)
    except (OSError, ValueError):
        return None


def terminate_job(identity: str) -> bool:
    try:
        api = _api()
        job = api.OpenJobObjectW(_QUERY | _TERMINATE, False, _job_name(identity))
        if not job:
            return getattr(ctypes, "get_last_error")() == 2
        try:
            return bool(api.TerminateJobObject(job, 125))
        finally:
            api.CloseHandle(job)
    except (OSError, ValueError):
        return False


def process_creation(handle: int) -> str:
    times = [ctypes.c_uint64() for _ in range(4)]
    if not _api().GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
        raise _error()
    return str(times[0].value)


def owner_creation() -> str:
    return process_creation(_api().GetCurrentProcess())


def open_owner(pid: int, creation: str) -> int:
    if pid <= 0 or not creation.isdecimal() or len(creation) > 20:
        raise ValueError("invalid Windows backend owner identity")
    api = _api()
    handle = api.OpenProcess(0x100000 | 0x1000, False, pid)
    if not handle:
        raise _error()
    try:
        if process_creation(handle) != creation or api.WaitForSingleObject(handle, 0) != 258:
            raise OSError("Windows backend owner incarnation changed or exited")
    except BaseException:
        api.CloseHandle(handle)
        raise
    return int(handle)


def wait_owner(handle: int) -> None:
    if _api().WaitForSingleObject(handle, 0xFFFFFFFF) != 0:
        raise _error()
