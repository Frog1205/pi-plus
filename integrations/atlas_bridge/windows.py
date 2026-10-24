"""Windows handle-based reads and fail-closed Job Object process ownership.

This constrains trusted Worker code with untrusted model/tool inputs. It is not
an AppContainer or an arbitrary hostile-code/network sandbox.
"""
from __future__ import annotations

import ctypes as ct
from ctypes import wintypes as w
import os
from pathlib import Path
import subprocess

if os.name != "nt":
    raise RuntimeError("Controlled P1 execution requires Windows Job Objects")

import msvcrt

k = ct.WinDLL("kernel32", use_last_error=True)
SIZE = ct.c_size_t


class Security(ct.Structure):
    _fields_ = [("length", w.DWORD), ("descriptor", w.LPVOID), ("inherit", w.BOOL)]


class BasicLimits(ct.Structure):
    _fields_ = [("process_time", ct.c_longlong), ("job_time", ct.c_longlong),
                ("flags", w.DWORD), ("min_working", SIZE), ("max_working", SIZE),
                ("active", w.DWORD), ("affinity", SIZE), ("priority", w.DWORD), ("scheduling", w.DWORD)]


class Counters(ct.Structure):
    _fields_ = [(name, ct.c_ulonglong) for name in ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]


class ExtendedLimits(ct.Structure):
    _fields_ = [("basic", BasicLimits), ("io", Counters), ("process_memory", SIZE),
                ("job_memory", SIZE), ("peak_process", SIZE), ("peak_job", SIZE)]


class Startup(ct.Structure):
    _fields_ = [("cb", w.DWORD), ("reserved", w.LPWSTR), ("desktop", w.LPWSTR), ("title", w.LPWSTR),
                ("x", w.DWORD), ("y", w.DWORD), ("width", w.DWORD), ("height", w.DWORD),
                ("xchars", w.DWORD), ("ychars", w.DWORD), ("fill", w.DWORD), ("flags", w.DWORD),
                ("show", w.WORD), ("reserved_size", w.WORD), ("reserved_bytes", ct.POINTER(ct.c_byte)),
                ("stdin", w.HANDLE), ("stdout", w.HANDLE), ("stderr", w.HANDLE)]


class ProcessInfo(ct.Structure):
    _fields_ = [("process", w.HANDLE), ("thread", w.HANDLE), ("pid", w.DWORD), ("tid", w.DWORD)]


def bind(name, args, result):
    function = getattr(k, name)
    function.argtypes = args
    function.restype = result
    return function


close_handle = bind("CloseHandle", [w.HANDLE], w.BOOL)
create_file = bind("CreateFileW", [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE], w.HANDLE)
final_path = bind("GetFinalPathNameByHandleW", [w.HANDLE, w.LPWSTR, w.DWORD, w.DWORD], w.DWORD)
file_type = bind("GetFileType", [w.HANDLE], w.DWORD)
create_job = bind("CreateJobObjectW", [w.LPVOID, w.LPCWSTR], w.HANDLE)
set_job = bind("SetInformationJobObject", [w.HANDLE, ct.c_int, w.LPVOID, w.DWORD], w.BOOL)
assign_job = bind("AssignProcessToJobObject", [w.HANDLE, w.HANDLE], w.BOOL)
terminate_job = bind("TerminateJobObject", [w.HANDLE, w.UINT], w.BOOL)
create_process = bind("CreateProcessW", [w.LPCWSTR, w.LPWSTR, w.LPVOID, w.LPVOID, w.BOOL, w.DWORD,
                                        w.LPVOID, w.LPCWSTR, ct.POINTER(Startup), ct.POINTER(ProcessInfo)], w.BOOL)
resume_thread = bind("ResumeThread", [w.HANDLE], w.DWORD)
terminate_process = bind("TerminateProcess", [w.HANDLE, w.UINT], w.BOOL)
wait_process = bind("WaitForSingleObject", [w.HANDLE, w.DWORD], w.DWORD)
get_exit = bind("GetExitCodeProcess", [w.HANDLE, ct.POINTER(w.DWORD)], w.BOOL)


def require(value):
    if not value:
        raise ct.WinError(ct.get_last_error())
    return value


def handle_path(handle) -> str:
    buffer = ct.create_unicode_buffer(32768)
    length = final_path(handle, buffer, len(buffer), 0)
    if not length or length >= len(buffer):
        raise ct.WinError(ct.get_last_error())
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return os.path.normcase(os.path.normpath(value))


def read_project_file(root: Path, relative: str, max_bytes: int = 65536) -> bytes:
    """Read from the attested open handle, never reopen an already checked path."""
    root_handle = create_file(str(root), 0x80, 1, None, 3, 0x02000000, None)
    if root_handle == ct.c_void_p(-1).value:
        raise ct.WinError(ct.get_last_error())
    try:
        trusted_root = handle_path(root_handle)
        path = root.joinpath(*relative.split("/"))
        handle = create_file(str(path), 0x80000000, 1, None, 3, 0x00200000, None)
        if handle == ct.c_void_p(-1).value:
            raise ct.WinError(ct.get_last_error())
        try:
            actual = handle_path(handle)
            if file_type(handle) != 1 or os.path.commonpath([trusted_root, actual]) != trusted_root:
                raise PermissionError("document is outside the registered project")
            # Reject leaf reparse points too. Parent junctions are validated by the
            # final handle path; switching them afterwards cannot change this handle.
            info = os.stat(path, follow_symlinks=False)
            if getattr(info, "st_file_attributes", 0) & 0x400:
                raise PermissionError("reparse document is not allowed")
            descriptor = msvcrt.open_osfhandle(int(handle), os.O_RDONLY | os.O_BINARY)
            handle = None  # ownership transferred to the descriptor
            with os.fdopen(descriptor, "rb") as stream:
                data = stream.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ValueError("document exceeds the project byte limit")
            return data
        finally:
            if handle is not None:
                close_handle(handle)
    finally:
        close_handle(root_handle)


class JobProcess:
    """Create suspended, assign a non-breakaway single-process job, then resume."""
    def __init__(self, arguments: list[str], cwd: Path, environment: dict[str, str]):
        self.job = require(create_job(None, None))
        self.process = None
        self.pid = 0
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000 | 0x8 | 0x100  # kill-on-close, active-process, process memory
        limits.basic.active = 1
        limits.process_memory = 1024 * 1024 * 1024
        unowned_fds = set()
        try:
            require(set_job(self.job, 9, ct.byref(limits), ct.sizeof(limits)))
            pairs = [os.pipe() for _ in range(3)]
            unowned_fds.update(fd for pair in pairs for fd in pair)
            child_fds = [pairs[0][0], pairs[1][1], pairs[2][1]]
            for descriptor in child_fds:
                os.set_handle_inheritable(msvcrt.get_osfhandle(descriptor), True)
            startup = Startup()
            startup.cb = ct.sizeof(startup)
            startup.flags = 0x100 | 0x1
            startup.show = 0
            startup.stdin, startup.stdout, startup.stderr = [msvcrt.get_osfhandle(fd) for fd in child_fds]
            info = ProcessInfo()
            command = ct.create_unicode_buffer(subprocess.list2cmdline(arguments))
            env = ct.create_unicode_buffer("\0".join(f"{key}={value}" for key, value in sorted(environment.items())) + "\0\0")
            try:
                require(create_process(arguments[0], command, None, None, True, 0x4 | 0x08000000 | 0x400,
                                       env, str(cwd), ct.byref(startup), ct.byref(info)))
                self.process, self.pid = info.process, info.pid
                try:
                    require(assign_job(self.job, self.process))
                    if resume_thread(info.thread) == 0xFFFFFFFF:
                        raise ct.WinError(ct.get_last_error())
                except BaseException:
                    terminate_process(self.process, 125)
                    raise
                finally:
                    close_handle(info.thread)
            finally:
                for descriptor in child_fds:
                    os.close(descriptor)
                    unowned_fds.discard(descriptor)
            self.stdin = os.fdopen(pairs[0][1], "wb", buffering=0)
            unowned_fds.discard(pairs[0][1])
            self.stdout = os.fdopen(pairs[1][0], "rb", buffering=0)
            unowned_fds.discard(pairs[1][0])
            self.stderr = os.fdopen(pairs[2][0], "rb", buffering=0)
            unowned_fds.discard(pairs[2][0])
        except BaseException:
            self.close()
            raise
        finally:
            for descriptor in unowned_fds:
                os.close(descriptor)

    def poll(self):
        if self.process is None:
            return None
        if wait_process(self.process, 0) == 0x102:
            return None
        code = w.DWORD()
        require(get_exit(self.process, ct.byref(code)))
        return code.value

    def kill(self):
        if self.job:
            terminate_job(self.job, 125)

    def close(self):
        if self.job:
            close_handle(self.job)
            self.job = None
        if self.process:
            wait_process(self.process, 5000)
            close_handle(self.process)
            self.process = None
        for name in ("stdin", "stdout", "stderr"):
            stream = getattr(self, name, None)
            if stream:
                stream.close()
