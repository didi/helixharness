"""Isolated subprocess trees on POSIX and Windows (kill-on-close Job Objects)."""

from __future__ import annotations

import os
import signal
import subprocess

_jobs: dict[int, object] = {}


def _windows_job(process: subprocess.Popen):
    # Imported lazily: ctypes.WinDLL is unavailable on POSIX.
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
            ("flags", wintypes.DWORD), ("minimum_ws", ctypes.c_size_t),
            ("maximum_ws", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
            ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
            ("scheduling", wintypes.DWORD),
        ]

    class Limits(ctypes.Structure):
        _fields_ = [
            ("basic", BasicLimits), ("io", ctypes.c_uint64 * 6),
            ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
            ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = Limits()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    try:
        if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel.AssignProcessToJobObject(handle, int(process._handle)):
            # A very short-lived command may finish before assignment.
            if process.poll() is None:
                raise ctypes.WinError(ctypes.get_last_error())
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    return lambda: kernel.CloseHandle(handle)


def spawn_group(command, **options) -> subprocess.Popen:
    """Launch a process tree whose cleanup does not depend on its leader surviving."""
    if os.name == "nt":
        options["creationflags"] = options.get("creationflags", 0) | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    process = subprocess.Popen(command, **options)
    if os.name == "nt":
        try:
            _jobs[process.pid] = _windows_job(process)
        except BaseException:
            process.kill()
            process.wait()
            raise
    return process


def kill_group(pid: int) -> None:
    """Stop an owned tree, including children left behind after the leader exits."""
    if os.name == "nt":
        close = _jobs.pop(pid, None)
        if close is not None:
            close()
        else:
            # Supervisor recovery for a PID registered by a different process.
            subprocess.run(["taskkill.exe", "/PID", str(pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
