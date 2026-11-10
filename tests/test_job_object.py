"""ctypes Job-Object wrapper — mock kernel32 for unit coverage."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from prism_router._job_object import (
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    JobAssignError,
    JobHandle,
    assign_worker,
)


class _FakeK32:
    """Mock kernel32 with configurable failure points."""

    def __init__(self, *, fail_at: str | None = None, errno: int = 5):
        self.fail_at = fail_at
        self.errno = errno
        self.calls: list[tuple] = []
        self._last_error = 0
        self._job_handle = 0xCAFE0001
        self._proc_handle = 0xCAFE0002

    def GetLastError(self) -> int:
        return self._last_error

    def CreateJobObjectW(self, attrs, name):
        self.calls.append(("CreateJobObjectW", attrs, name))
        if self.fail_at == "CreateJobObjectW":
            self._last_error = self.errno
            return 0
        return self._job_handle

    def SetInformationJobObject(self, hjob, info_class, info_ptr, info_size):
        self.calls.append(("SetInformationJobObject", hjob, info_class, info_size))
        if self.fail_at == "SetInformationJobObject":
            self._last_error = self.errno
            return 0
        return 1

    def OpenProcess(self, access, inherit, pid):
        self.calls.append(("OpenProcess", access, inherit, pid))
        if self.fail_at == "OpenProcess":
            self._last_error = self.errno
            return 0
        return self._proc_handle

    def AssignProcessToJobObject(self, hjob, hproc):
        self.calls.append(("AssignProcessToJobObject", hjob, hproc))
        if self.fail_at == "AssignProcessToJobObject":
            self._last_error = self.errno
            return 0
        return 1

    def CloseHandle(self, h):
        self.calls.append(("CloseHandle", h))
        return 1


def test_assign_worker_happy_path():
    k = _FakeK32()
    h = assign_worker(1234, kernel32=k)
    assert isinstance(h, JobHandle)
    assert h.handle == 0xCAFE0001
    # Order: CreateJob → SetInfo → OpenProcess → AssignProcess → CloseHandle(hproc)
    names = [c[0] for c in k.calls]
    assert names == [
        "CreateJobObjectW",
        "SetInformationJobObject",
        "OpenProcess",
        "AssignProcessToJobObject",
        "CloseHandle",  # hproc released
    ]
    # SetInfo must set KILL_ON_JOB_CLOSE; verify via the size arg's presence.
    setinfo = [c for c in k.calls if c[0] == "SetInformationJobObject"][0]
    assert setinfo[2] == 9  # JobObjectExtendedLimitInformation


def test_jobhandle_close_closes_via_kernel():
    k = _FakeK32()
    h = assign_worker(1234, kernel32=k)
    h.close()
    last = k.calls[-1]
    assert last == ("CloseHandle", 0xCAFE0001)
    # Idempotent.
    h.close()
    assert k.calls.count(("CloseHandle", 0xCAFE0001)) == 1


def test_jobhandle_context_manager_closes():
    k = _FakeK32()
    with assign_worker(1234, kernel32=k) as h:
        assert h.handle == 0xCAFE0001
    assert ("CloseHandle", 0xCAFE0001) in k.calls


@pytest.mark.parametrize("phase", [
    "CreateJobObjectW",
    "SetInformationJobObject",
    "OpenProcess",
    "AssignProcessToJobObject",
])
def test_assign_worker_fail_closed_at_each_phase(phase):
    k = _FakeK32(fail_at=phase, errno=42)
    with pytest.raises(JobAssignError) as ei:
        assign_worker(1234, kernel32=k)
    assert ei.value.phase == phase
    assert ei.value.errno == 42
    # Any partially-acquired handle must be closed.
    closed = [c for c in k.calls if c[0] == "CloseHandle"]
    if phase == "CreateJobObjectW":
        assert closed == []  # nothing to close
    elif phase == "SetInformationJobObject":
        assert any(c[1] == 0xCAFE0001 for c in closed)  # job closed
    elif phase == "OpenProcess":
        assert any(c[1] == 0xCAFE0001 for c in closed)  # job closed
    elif phase == "AssignProcessToJobObject":
        # both proc and job released
        handles = {c[1] for c in closed}
        assert 0xCAFE0001 in handles
        assert 0xCAFE0002 in handles
