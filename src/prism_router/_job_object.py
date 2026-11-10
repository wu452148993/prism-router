"""Windows Job Object ctypes wrapper.

Creates a Job Object with KILL_ON_JOB_CLOSE, opens a Win32 handle for the
worker pid, and atomically assigns the worker. On `CloseHandle(job)` the OS
terminates the worker process and every descendant — the universal kill
switch for the dispatcher.

Fail-closed: any kernel32 error raises `JobAssignError`; caller must
TerminateProcess(worker_pid) and return spawn_failed. The dispatcher MUST NOT
write stdin to a worker that has not been assigned to a Job.

Test seam: `_get_kernel32()` is a factory the tests monkeypatch with a mock
exposing the same call surface (CreateJobObjectW, SetInformationJobObject,
OpenProcess, AssignProcessToJobObject, CloseHandle, GetLastError).
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

# Windows constants
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JobObjectExtendedLimitInformation = 9
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


# Struct layouts match winnt.h JOBOBJECT_EXTENDED_LIMIT_INFORMATION exactly.
class _JOBOBJECT_BASIC_LIMIT(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_ulong),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_ulong),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_ulong),
        ("SchedulingClass", ctypes.c_ulong),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_EXTENDED_LIMIT(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class JobAssignError(Exception):
    """Job setup failed. `phase` identifies which kernel32 call; `errno` is
    GetLastError at failure point. Caller must fail-closed: terminate the
    worker via TerminateProcess(pid) and return spawn_failed envelope."""

    def __init__(self, phase: str, errno: int):
        self.phase = phase
        self.errno = errno
        super().__init__(f"{phase} failed: GetLastError={errno}")


class JobHandle:
    """Owns a Job Object handle. `close()` closes the handle, which on Windows
    with KILL_ON_JOB_CLOSE terminates worker + every descendant atomically."""

    def __init__(self, handle: int, kernel32):
        self._handle = handle
        self._k = kernel32

    @property
    def handle(self) -> int:
        return self._handle

    def close(self) -> None:
        if self._handle:
            self._k.CloseHandle(self._handle)
            self._handle = 0

    def __enter__(self) -> "JobHandle":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


_kernel32_cache = None


def _get_kernel32():
    """Return a kernel32 reference with explicit ctypes prototypes.

    Without restype/argtypes declarations, ctypes defaults restype=c_int (32-bit
    signed). On 64-bit Windows, HANDLE is a 64-bit pointer; the default would
    truncate every returned handle, breaking CloseHandle / AssignProcessToJobObject
    against silently-mangled values. We declare prototypes on a private WinDLL
    instance to avoid polluting the shared `ctypes.windll.kernel32`.
    """
    global _kernel32_cache
    if sys.platform != "win32":
        raise OSError("Job Object support requires Windows")
    if _kernel32_cache is not None:
        return _kernel32_cache
    k = ctypes.WinDLL("kernel32", use_last_error=True)

    k.CreateJobObjectW.restype = wintypes.HANDLE
    k.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]

    k.SetInformationJobObject.restype = wintypes.BOOL
    k.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
    ]

    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]

    k.AssignProcessToJobObject.restype = wintypes.BOOL
    k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

    k.CloseHandle.restype = wintypes.BOOL
    k.CloseHandle.argtypes = [wintypes.HANDLE]

    k.GetLastError.restype = wintypes.DWORD
    k.GetLastError.argtypes = []

    _kernel32_cache = k
    return k


def assign_worker(pid: int, *, kernel32=None) -> JobHandle:
    """Create Job (KILL_ON_JOB_CLOSE, non-inheritable, no breakaway) and
    assign worker pid. Returns owning JobHandle.

    Sync; no awaits. Caller MUST invoke before writing worker stdin. On any
    failure, no Job exists and pid has been opened-then-released — caller
    fails-closed via TerminateProcess(pid).
    """
    k = kernel32 or _get_kernel32()

    # lpJobAttributes=NULL (non-inheritable default), lpName=NULL.
    hjob = k.CreateJobObjectW(None, None)
    if not hjob:
        raise JobAssignError("CreateJobObjectW", k.GetLastError())

    limit = _JOBOBJECT_EXTENDED_LIMIT()
    limit.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not k.SetInformationJobObject(
        hjob,
        JobObjectExtendedLimitInformation,
        ctypes.byref(limit),
        ctypes.sizeof(limit),
    ):
        errno = k.GetLastError()
        k.CloseHandle(hjob)
        raise JobAssignError("SetInformationJobObject", errno)

    hproc = k.OpenProcess(
        PROCESS_SET_QUOTA | PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION,
        False,
        pid,
    )
    if not hproc:
        errno = k.GetLastError()
        k.CloseHandle(hjob)
        raise JobAssignError("OpenProcess", errno)

    if not k.AssignProcessToJobObject(hjob, hproc):
        errno = k.GetLastError()
        k.CloseHandle(hproc)
        k.CloseHandle(hjob)
        raise JobAssignError("AssignProcessToJobObject", errno)

    # Job retains its own ref to the assigned process; safe to release hproc.
    k.CloseHandle(hproc)
    return JobHandle(hjob, k)
