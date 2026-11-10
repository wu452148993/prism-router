"""Per-call parent → child lineage rows (docs/lineage.md).

Each tool call appends up to three self-contained jsonl rows — `start` before
any backend work, `child` the instant the child session id is known, `end`
when the call settles — so agent-os can hang every codex/gemini session under
the Claude Code session that spawned it. Side channel only: nothing here may
change what a call returns, a failed write is swallowed (debug log), and rows
are never read back. Windows-only, like dispatch itself
(docs/bugs/non-windows-dispatch-unsupported.md).
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import os
import sys
import time
import uuid
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, TypeVar

from . import __version__
from .config import _bool_env
from .paths import normalize_cwd

_LOG = logging.getLogger("prism_router.lineage")

V = 1
MAX_LINE_BYTES = 4096
# The dispatcher's three wall-clock watchdogs; every other envelope error is
# outcome "error". The precise code always rides along in `error_code`.
_TIMEOUT_CODES = frozenset({"timeout", "pre_terminal_idle", "post_terminal_grace"})
_CLAUDE_EXES = frozenset({"claude", "claude.exe"})

_T = TypeVar("_T", bound=Mapping[str, Any])


@dataclass(frozen=True)
class _Context:
    directory: Path
    fields: dict[str, Any]  # process-lifetime row fields, in row order


_ctx: _Context | None = None


def configure(env: Mapping[str, str] | None = None) -> None:
    """Read the knobs and this process's identity once, from `main()`.

    Lineage stays off when `PRISM_LINEAGE` disables it, off Windows, or on any
    failure. Never raises.
    """
    global _ctx
    _ctx = None
    env = os.environ if env is None else env
    if sys.platform != "win32" or not _bool_env(env, "PRISM_LINEAGE", True):
        return
    try:
        directory = Path(env.get("PRISM_LINEAGE_DIR") or Path.home() / ".prism" / "lineage")
        parent_pid, parent_start, parent_exe = _parent()
        _ctx = _Context(directory, {
            "project_dir": env.get("CLAUDE_PROJECT_DIR") or None,
            "prism_pid": os.getpid(),
            "prism_version": __version__,
            # Spawn-time value: Claude Code does not refresh it after /clear.
            "parent_session_id_env": env.get("CLAUDE_CODE_SESSION_ID") or None,
            "parent_pid": parent_pid,
            "parent_pid_start": parent_start,
            "parent_exe": parent_exe,
        })
    except Exception as exc:  # noqa: BLE001 — lineage must never block startup
        _quiet("disabled", exc)


def _quiet(what: str, exc: Exception) -> None:
    """DEBUG-log a swallowed failure. The log call is guarded too: a failing
    handler must not carry the failure into the call it was hiding from."""
    try:
        _LOG.debug("lineage %s: %r", what, exc)
    except Exception:  # noqa: BLE001
        pass


async def track(
    backend: str,
    cd: Any,
    session_id: str,
    invoke: Callable[[Callable[[str], None] | None], Awaitable[_T]],
) -> _T:
    """Run one tool call between its `start` and `end` rows.

    `invoke` receives the `child` callback (None when lineage is off) and its
    envelope is returned untouched. Exceptions are re-raised as-is after their
    `end` row.
    """
    call = _Call.begin(backend, cd, session_id)
    if call is None:
        return await invoke(None)
    try:
        envelope = await invoke(call.child)
    except asyncio.CancelledError:
        call.end("cancelled")
        raise
    except Exception:
        call.end("error")
        raise
    except BaseException:  # interpreter teardown: KeyboardInterrupt / SystemExit / GeneratorExit
        call.end("killed")
        raise
    call.settle(envelope)
    return envelope


class _Call:
    """The rows of one tool call. No method raises (an `Exception`)."""

    def __init__(self, ctx: _Context, backend: str, cwd: str, requested: str | None):
        self._ctx = ctx
        self._t0 = time.monotonic()
        self._ids = {"call_id": str(uuid.uuid4()), "backend": backend, "cwd": cwd}
        self._requested = requested
        self._child: str | None = None
        self._child_written = False
        self._ended = False

    @classmethod
    def begin(cls, backend: str, cd: Any, session_id: str) -> "_Call | None":
        if _ctx is None:
            return None
        try:
            call = cls(_ctx, backend, str(normalize_cwd(cd)), session_id or None)
        except Exception as exc:  # noqa: BLE001
            _quiet("start skipped", exc)
            return None
        call._emit("start")
        return call

    def child(self, session_id: str) -> None:
        """Latest known child id; the first one also writes the `child` row.
        Ignored once the call has ended: a stderr drain the dispatcher left
        running past a cancel must not append to a closed call."""
        if self._ended:
            return
        self._child = session_id
        if not self._child_written:
            self._child_written = True
            self._emit("child")

    def settle(self, envelope: Mapping[str, Any]) -> None:
        try:
            code = (envelope.get("error") or {}).get("code")
            outcome = (
                "success" if envelope.get("success")
                else "timeout" if code in _TIMEOUT_CODES
                else "error"
            )
            sid = envelope.get("SESSION_ID")
        except Exception as exc:  # noqa: BLE001 — not an envelope; still close the call
            _quiet("envelope unreadable", exc)
            code, outcome, sid = None, "error", None
        if sid:
            # The id handed to the caller is final. It also backfills the
            # `child` row when the result frame beat the milestone.
            self.child(sid)
        self.end(outcome, code)

    def end(self, outcome: str, error_code: str | None = None) -> None:
        if self._ended:
            return
        self._ended = True
        self._emit(
            "end", outcome=outcome, error_code=error_code,
            duration_ms=int((time.monotonic() - self._t0) * 1000),
        )

    def _emit(self, event: str, **extra: Any) -> None:
        try:
            ts = _iso(datetime.now(timezone.utc))
            row = {
                "v": V, "event": event, "ts": ts, **self._ids, **self._ctx.fields,
                "requested_session_id": self._requested,
                "child_session_id": self._child,
                "resumed": None if self._child is None else self._child == self._requested,
                **extra,
            }
            data = json.dumps(row, ensure_ascii=True, separators=(",", ":")).encode() + b"\n"
            if len(data) > MAX_LINE_BYTES:
                # Inputs are not length-capped. Normal ones (paths within
                # MAX_PATH, UUID session ids, ordinary parent process names)
                # top out near 3.7 KB; a row past the cap comes from abnormal
                # input and is surfaced, not written.
                _LOG.warning(
                    "lineage %s row of call %s dropped: %d bytes > %d",
                    event, self._ids["call_id"], len(data), MAX_LINE_BYTES,
                )
                return
            self._ctx.directory.mkdir(parents=True, exist_ok=True)
            _append(self._ctx.directory / f"{ts[:7]}.jsonl", data)
        except Exception as exc:  # noqa: BLE001 — a lost row never fails the call
            _quiet(f"{event} row not written", exc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ───── Windows primitives (ctypes; no runtime dependency) ─────

_FILE_APPEND_DATA = 0x0004
_FILE_SHARE_ALL = 0x0007  # READ | WRITE | DELETE
_OPEN_ALWAYS = 4
_FILE_ATTRIBUTE_NORMAL = 0x80
_TH32CS_SNAPPROCESS = 0x2
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


_kernel32_cache = None


def _kernel32():
    """Private kernel32 with explicit prototypes: the default c_int restype
    would truncate 64-bit HANDLEs (same rationale as `_job_object`)."""
    global _kernel32_cache
    if _kernel32_cache is None:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateFileW.restype = wintypes.HANDLE
        k.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        k.WriteFile.restype = wintypes.BOOL
        k.WriteFile.argtypes = [
            wintypes.HANDLE, ctypes.c_char_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
        ]
        k.CloseHandle.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        for walk in (k.Process32FirstW, k.Process32NextW):
            walk.restype = wintypes.BOOL
            walk.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
        k.OpenProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.GetProcessTimes.restype = wintypes.BOOL
        k.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        _kernel32_cache = k
    return _kernel32_cache


def _append(path: Path, data: bytes) -> None:
    """One `WriteFile` on a handle holding FILE_APPEND_DATA without
    FILE_WRITE_DATA. For local files, Microsoft documents that such writes
    land at end-of-file without overwriting existing data; that concurrent
    appenders never interleave was measured on NTFS (docs/lineage.md).
    `os.open(O_APPEND)` is not equivalent: the CRT emulates it as seek-to-end
    then write, which lost and tore lines in the same measurement."""
    k = _kernel32()
    handle = k.CreateFileW(
        str(path), _FILE_APPEND_DATA, _FILE_SHARE_ALL, None,
        _OPEN_ALWAYS, _FILE_ATTRIBUTE_NORMAL, None,
    )
    if handle == _INVALID_HANDLE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        written = wintypes.DWORD()
        if not k.WriteFile(handle, data, len(data), ctypes.byref(written), None):
            raise ctypes.WinError(ctypes.get_last_error())
        if written.value != len(data):
            raise OSError(f"short write: {written.value} of {len(data)} bytes")
    finally:
        k.CloseHandle(handle)


def _parent() -> tuple[int, str | None, str | None]:
    """`(pid, creation time, exe)` of the nearest `claude` ancestor, else of the
    direct parent. The creation time is None when it cannot be read or the
    chain up to it could not be age-checked."""
    try:
        return _find_parent(os.getpid(), _process_table(), _created)
    except Exception as exc:  # noqa: BLE001
        _quiet("parent walk failed", exc)
        return os.getppid(), None, None


def _find_parent(
    pid: int,
    table: Mapping[int, tuple[int, str]],
    created: Callable[[int], int | None],
) -> tuple[int, str | None, str | None]:
    """Walk `table` ({pid: (ppid, exe)}) up from `pid`. Each hop must add an
    unseen pid from a finite table, so the walk terminates."""
    direct = None
    seen = {pid}
    bound = created(pid)  # creation time of the youngest readable descendant
    checked = bound is not None  # every link so far was age-checked
    while pid in table:
        ppid = table[pid][0]
        if ppid in seen or ppid not in table:
            break
        ppid_created = created(ppid)
        # Windows never reparents: an "ancestor" younger than a descendant is
        # an unrelated process that reused a dead ancestor's pid.
        if bound is not None and ppid_created is not None and ppid_created > bound:
            break
        checked = checked and ppid_created is not None
        hop = (ppid, _filetime_iso(ppid_created) if checked else None, table[ppid][1])
        if direct is None:
            direct = hop
        if hop[2].lower() in _CLAUDE_EXES:
            return hop
        seen.add(ppid)
        pid = ppid
        if ppid_created is not None:
            bound = ppid_created
    return direct or (os.getppid(), None, None)


def _process_table() -> dict[int, tuple[int, str]]:
    k = _kernel32()
    snapshot = k.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snapshot == _INVALID_HANDLE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        entry = _PROCESSENTRY32W(dwSize=ctypes.sizeof(_PROCESSENTRY32W))
        table: dict[int, tuple[int, str]] = {}
        more = k.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            table[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile)
            more = k.Process32NextW(snapshot, ctypes.byref(entry))
        return table
    finally:
        k.CloseHandle(snapshot)


def _created(pid: int) -> int | None:
    """Process creation time as a FILETIME tick count, or None."""
    k = _kernel32()
    handle = k.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not k.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return None
        return times[0].dwHighDateTime << 32 | times[0].dwLowDateTime
    finally:
        k.CloseHandle(handle)


def _filetime_iso(ticks: int | None) -> str | None:
    if ticks is None:
        return None
    return _iso(_FILETIME_EPOCH + timedelta(microseconds=ticks // 10))
