"""Win32 plumbing for running the agent the way Parsec/Sunshine-style hosts do.

A watchdog running as SYSTEM (session 0, started at boot) launches the agent into the active
console session with a SYSTEM token. From there the agent can:
  - follow the *input desktop*: "Default" normally, "Winlogon" for the sign-in/lock screen and
    UAC prompts. Capture (ffmpeg ddagrab) is started on that desktop, and input is sent from a
    thread attached to it;
  - inject input into elevated windows (SYSTEM outranks them);
  - generate a real Ctrl+Alt+Del with SendSAS.
Everything binds lazily so this module imports on any OS (the tests use that).
"""
import asyncio
import ctypes
import functools
import os
import subprocess
import threading
from ctypes import wintypes as W

MAXIMUM_ALLOWED = 0x02000000
GENERIC_ALL = 0x10000000
UOI_NAME = 2
STARTF_USESHOWWINDOW, STARTF_USESTDHANDLES = 0x1, 0x100
CREATE_SUSPENDED, CREATE_NO_WINDOW, CREATE_UNICODE_ENVIRONMENT = 0x4, 0x08000000, 0x400
HANDLE_FLAG_INHERIT = 0x1
INFINITE, WAIT_TIMEOUT = 0xFFFFFFFF, 0x102
TOKEN_ALL_ACCESS, TOKEN_ADJUST_PRIVILEGES, TOKEN_QUERY = 0xF01FF, 0x20, 0x8
SecurityImpersonation, TokenPrimary, TokenSessionId = 2, 1, 12
SE_PRIVILEGE_ENABLED = 0x2
JobObjectExtendedLimitInformation, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 9, 0x2000


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", W.DWORD), ("lpSecurityDescriptor", W.LPVOID), ("bInheritHandle", W.BOOL)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", W.DWORD), ("lpReserved", W.LPWSTR), ("lpDesktop", W.LPWSTR), ("lpTitle", W.LPWSTR),
                ("dwX", W.DWORD), ("dwY", W.DWORD), ("dwXSize", W.DWORD), ("dwYSize", W.DWORD),
                ("dwXCountChars", W.DWORD), ("dwYCountChars", W.DWORD), ("dwFillAttribute", W.DWORD),
                ("dwFlags", W.DWORD), ("wShowWindow", W.WORD), ("cbReserved2", W.WORD),
                ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
                ("hStdInput", W.HANDLE), ("hStdOutput", W.HANDLE), ("hStdError", W.HANDLE)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", W.HANDLE), ("hThread", W.HANDLE), ("dwProcessId", W.DWORD), ("dwThreadId", W.DWORD)]


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", W.DWORD), ("HighPart", W.LONG)]


class LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", LUID), ("Attributes", W.DWORD)]


class TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [("PrivilegeCount", W.DWORD), ("Privileges", LUID_AND_ATTRIBUTES * 1)]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                    "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", W.LARGE_INTEGER), ("PerJobUserTimeLimit", W.LARGE_INTEGER),
                ("LimitFlags", W.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", W.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", W.DWORD), ("SchedulingClass", W.DWORD)]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION), ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


def _fn(dll, name, restype, *argtypes):
    f = getattr(dll, name)
    f.restype, f.argtypes = restype, argtypes
    return f


@functools.lru_cache(maxsize=None)
def api():
    """Bind the Win32 functions once, with explicit types (64-bit handles must not truncate)."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    H, D, B, P = W.HANDLE, W.DWORD, W.BOOL, ctypes.POINTER
    return {
        "OpenInputDesktop": _fn(u32, "OpenInputDesktop", H, D, B, D),
        "CloseDesktop": _fn(u32, "CloseDesktop", B, H),
        "SetThreadDesktop": _fn(u32, "SetThreadDesktop", B, H),
        "GetUserObjectInformationW": _fn(u32, "GetUserObjectInformationW", B, H, ctypes.c_int, W.LPVOID, D, P(D)),
        "CreatePipe": _fn(k32, "CreatePipe", B, P(H), P(H), P(SECURITY_ATTRIBUTES), D),
        "SetHandleInformation": _fn(k32, "SetHandleInformation", B, H, D, D),
        "ReadFile": _fn(k32, "ReadFile", B, H, W.LPVOID, D, P(D), W.LPVOID),
        "WriteFile": _fn(k32, "WriteFile", B, H, W.LPCVOID, D, P(D), W.LPVOID),
        "CloseHandle": _fn(k32, "CloseHandle", B, H),
        "CreateProcessW": _fn(k32, "CreateProcessW", B, W.LPCWSTR, W.LPWSTR, W.LPVOID, W.LPVOID, B, D,
                              W.LPVOID, W.LPCWSTR, P(STARTUPINFOW), P(PROCESS_INFORMATION)),
        "TerminateProcess": _fn(k32, "TerminateProcess", B, H, W.UINT),
        "WaitForSingleObject": _fn(k32, "WaitForSingleObject", D, H, D),
        "GetExitCodeProcess": _fn(k32, "GetExitCodeProcess", B, H, P(D)),
        "ResumeThread": _fn(k32, "ResumeThread", D, H),
        "GetCurrentProcess": _fn(k32, "GetCurrentProcess", H),
        "WTSGetActiveConsoleSessionId": _fn(k32, "WTSGetActiveConsoleSessionId", D),
        "CreateJobObjectW": _fn(k32, "CreateJobObjectW", H, W.LPVOID, W.LPCWSTR),
        "SetInformationJobObject": _fn(k32, "SetInformationJobObject", B, H, ctypes.c_int, W.LPVOID, D),
        "AssignProcessToJobObject": _fn(k32, "AssignProcessToJobObject", B, H, H),
        "OpenProcessToken": _fn(adv, "OpenProcessToken", B, H, D, P(H)),
        "DuplicateTokenEx": _fn(adv, "DuplicateTokenEx", B, H, D, W.LPVOID, ctypes.c_int, ctypes.c_int, P(H)),
        "SetTokenInformation": _fn(adv, "SetTokenInformation", B, H, ctypes.c_int, W.LPVOID, D),
        "LookupPrivilegeValueW": _fn(adv, "LookupPrivilegeValueW", B, W.LPCWSTR, W.LPCWSTR, P(LUID)),
        "AdjustTokenPrivileges": _fn(adv, "AdjustTokenPrivileges", B, H, B, P(TOKEN_PRIVILEGES), D, W.LPVOID, W.LPVOID),
        "CreateProcessAsUserW": _fn(adv, "CreateProcessAsUserW", B, H, W.LPCWSTR, W.LPWSTR, W.LPVOID, W.LPVOID,
                                    B, D, W.LPVOID, W.LPCWSTR, P(STARTUPINFOW), P(PROCESS_INFORMATION)),
    }


def _err(what):
    return OSError(ctypes.get_last_error(), f"{what} failed (error {ctypes.get_last_error()})")


# ----------------------------------------------------------------------------------------------
# Input desktop
# ----------------------------------------------------------------------------------------------

def input_desktop_name():
    """'Default', 'Winlogon' (sign-in/lock screen, UAC), or None if we may not look."""
    a = api()
    h = a["OpenInputDesktop"](0, False, MAXIMUM_ALLOWED)
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(256)
        need = W.DWORD()
        if not a["GetUserObjectInformationW"](h, UOI_NAME, buf, ctypes.sizeof(buf), ctypes.byref(need)):
            return None
        return buf.value
    finally:
        a["CloseDesktop"](h)


class ThreadDesktop:
    """Keeps the calling thread attached to the current input desktop (one per thread)."""

    def __init__(self):
        self.handle = None
        self.name = None

    def ensure(self):
        name = input_desktop_name()
        if name is None or name == self.name:
            return
        a = api()
        h = a["OpenInputDesktop"](0, False, GENERIC_ALL)
        if not h:
            return
        if a["SetThreadDesktop"](h):
            if self.handle:
                a["CloseDesktop"](self.handle)
            self.handle, self.name = h, name
        else:
            a["CloseDesktop"](h)


def send_sas():
    """Ctrl+Alt+Del. Needs a SYSTEM caller and the SoftwareSASGeneration policy (setup sets it)."""
    try:
        fn = ctypes.WinDLL("sas.dll").SendSAS
    except (OSError, AttributeError):
        return False
    fn.argtypes, fn.restype = (W.BOOL,), None
    fn(False)              # FALSE = the caller is not a user-mode app pretending to be one
    return True


# ----------------------------------------------------------------------------------------------
# Spawning ffmpeg on a specific desktop, with asyncio-friendly pipes
# ----------------------------------------------------------------------------------------------

def _pipe(parent_reads):
    a = api()
    sa = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), None, True)
    r, w = W.HANDLE(), W.HANDLE()
    if not a["CreatePipe"](ctypes.byref(r), ctypes.byref(w), ctypes.byref(sa), 1 << 20):
        raise _err("CreatePipe")
    a["SetHandleInformation"](r if parent_reads else w, HANDLE_FLAG_INHERIT, 0)   # parent end stays private
    return r, w


class _PipeWriter:
    """Just enough of asyncio.StreamWriter for the audio feed (write + drain)."""

    def __init__(self, h):
        self.h = h

    def write(self, data):
        n = W.DWORD()
        if not api()["WriteFile"](self.h, data, len(data), ctypes.byref(n), None):
            raise BrokenPipeError("pipe closed")

    async def drain(self):
        pass

    def close(self):
        api()["CloseHandle"](self.h)


class Win32Process:
    """Mimics asyncio.subprocess.Process for media.FFmpegStream."""

    def __init__(self, pi, out_r, err_r, in_w):
        self._h = pi.hProcess
        api()["CloseHandle"](pi.hThread)
        self.pid = pi.dwProcessId
        self.returncode = None
        loop = asyncio.get_running_loop()
        self.stdout, self.stderr = asyncio.StreamReader(limit=1 << 22), asyncio.StreamReader()
        self.stdin = _PipeWriter(in_w) if in_w else None
        for h, reader in ((out_r, self.stdout), (err_r, self.stderr)):
            threading.Thread(target=self._pump, args=(h, reader, loop), daemon=True).start()

    @staticmethod
    def _pump(h, reader, loop):
        a = api()
        buf = ctypes.create_string_buffer(1 << 16)
        n = W.DWORD()
        try:
            while a["ReadFile"](h, buf, len(buf), ctypes.byref(n), None) and n.value:
                loop.call_soon_threadsafe(reader.feed_data, buf.raw[:n.value])
        except RuntimeError:          # loop closed
            pass
        finally:
            a["CloseHandle"](h)
            try:
                loop.call_soon_threadsafe(reader.feed_eof)
            except RuntimeError:
                pass

    def kill(self):
        if self.returncode is None:
            api()["TerminateProcess"](self._h, 1)

    def _wait_blocking(self):
        a = api()
        a["WaitForSingleObject"](self._h, INFINITE)
        code = W.DWORD()
        a["GetExitCodeProcess"](self._h, ctypes.byref(code))
        return code.value

    async def wait(self):
        if self.returncode is None:
            self.returncode = await asyncio.get_running_loop().run_in_executor(None, self._wait_blocking)
            if self.stdin:
                self.stdin.close()
            api()["CloseHandle"](self._h)
        return self.returncode


async def spawn_on_input_desktop(cmd, stdin=False):
    """Start cmd on whichever desktop is receiving input right now (Default or Winlogon)."""
    a = api()
    desktop = "winsta0\\" + (input_desktop_name() or "Default")
    out_r, out_w = _pipe(True)
    err_r, err_w = _pipe(True)
    in_r = in_w = None
    if stdin:
        in_r, in_w = _pipe(False)
    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(si)
    si.lpDesktop = desktop
    si.dwFlags = STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    si.hStdInput, si.hStdOutput, si.hStdError = in_r, out_w, err_w
    pi = PROCESS_INFORMATION()
    cmdline = ctypes.create_unicode_buffer(subprocess.list2cmdline(cmd))
    ok = a["CreateProcessW"](None, cmdline, None, None, True, CREATE_NO_WINDOW, None, None,
                             ctypes.byref(si), ctypes.byref(pi))
    for h in (out_w, err_w, in_r):
        if h:
            a["CloseHandle"](h)
    if not ok:
        for h in (out_r, err_r, in_w):
            if h:
                a["CloseHandle"](h)
        raise _err("CreateProcessW")
    return Win32Process(pi, out_r, err_r, in_w)


# ----------------------------------------------------------------------------------------------
# Watchdog side: launch the agent into a session as SYSTEM
# ----------------------------------------------------------------------------------------------

def enable_privileges(*names):
    a = api()
    tok = W.HANDLE()
    if not a["OpenProcessToken"](a["GetCurrentProcess"](), TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY, ctypes.byref(tok)):
        raise _err("OpenProcessToken")
    try:
        for name in names:
            tp = TOKEN_PRIVILEGES(1)
            if a["LookupPrivilegeValueW"](None, name, ctypes.byref(tp.Privileges[0].Luid)):
                tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
                a["AdjustTokenPrivileges"](tok, False, ctypes.byref(tp), 0, None, None)
    finally:
        a["CloseHandle"](tok)


def active_console_session():
    sid = api()["WTSGetActiveConsoleSessionId"]()
    return None if sid == 0xFFFFFFFF else sid


class SessionProcess:
    """A process started in another session with a copy of our (SYSTEM) token, inside a job
    object so it - and every ffmpeg/cloudflared it starts - dies together when we kill it."""

    def __init__(self, cmd, session_id, cwd=None, desktop="winsta0\\default"):
        a = api()
        own, tok = W.HANDLE(), W.HANDLE()
        if not a["OpenProcessToken"](a["GetCurrentProcess"](), TOKEN_ALL_ACCESS, ctypes.byref(own)):
            raise _err("OpenProcessToken")
        try:
            if not a["DuplicateTokenEx"](own, MAXIMUM_ALLOWED, None, SecurityImpersonation, TokenPrimary,
                                         ctypes.byref(tok)):
                raise _err("DuplicateTokenEx")
        finally:
            a["CloseHandle"](own)
        try:
            sid = W.DWORD(session_id)
            if not a["SetTokenInformation"](tok, TokenSessionId, ctypes.byref(sid), ctypes.sizeof(sid)):
                raise _err("SetTokenInformation(TokenSessionId)")
            si = STARTUPINFOW()
            si.cb = ctypes.sizeof(si)
            si.lpDesktop = desktop
            pi = PROCESS_INFORMATION()
            cmdline = ctypes.create_unicode_buffer(subprocess.list2cmdline(cmd))
            if not a["CreateProcessAsUserW"](tok, None, cmdline, None, None, False,
                                             CREATE_SUSPENDED | CREATE_NO_WINDOW, None, cwd,
                                             ctypes.byref(si), ctypes.byref(pi)):
                raise _err("CreateProcessAsUserW")
        finally:
            a["CloseHandle"](tok)
        self.job = a["CreateJobObjectW"](None, None)
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        a["SetInformationJobObject"](self.job, JobObjectExtendedLimitInformation,
                                     ctypes.byref(info), ctypes.sizeof(info))
        a["AssignProcessToJobObject"](self.job, pi.hProcess)
        a["ResumeThread"](pi.hThread)
        a["CloseHandle"](pi.hThread)
        self.handle, self.pid, self.session = pi.hProcess, pi.dwProcessId, session_id

    def wait(self, timeout_ms):
        """True if the process has exited."""
        return api()["WaitForSingleObject"](self.handle, timeout_ms) != WAIT_TIMEOUT

    def kill(self):
        a = api()
        a["CloseHandle"](self.job)          # KILL_ON_JOB_CLOSE takes the whole tree down
        a["TerminateProcess"](self.handle, 1)
        a["CloseHandle"](self.handle)
