"""ConPTY transport for Windows — stdlib ctypes only, no third-party packages.

Windows 10 1809+ (build 17763) ships a real pseudo-console API (ConPTY) that is
the direct analogue of POSIX openpty: a console device whose input/output are
pipes, so a full TUI (Claude Code, PowerShell) renders correctly.

This module exposes one class, ConPtyProcess, with the same handful of
operations server.py needs from a PTY: read / write / set_size / close / alive.

Import is safe on every platform: everything Windows-specific is resolved
lazily, and AVAILABLE tells the caller whether spawning can work here.
"""
import os
import subprocess
import sys

AVAILABLE = sys.platform == "win32"

# Win32 constants
_S_OK = 0
_PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_STILL_ACTIVE = 259
_ERROR_BROKEN_PIPE = 109


def build_command_line(argv):
    """Quote an argv list into a Windows command line (testable anywhere)."""
    return subprocess.list2cmdline(list(argv))


def build_environment_block(env):
    """Windows environment block: 'K=V\\0K=V\\0\\0' as UTF-16 (testable)."""
    parts = []
    for k, v in env.items():
        if k and "=" not in k:
            parts.append(f"{k}={v}")
    return ("\0".join(sorted(parts)) + "\0\0").encode("utf-16-le")


def unsupported_reason():
    """None if ConPTY can be used here, else a human-readable reason."""
    if sys.platform != "win32":
        return "not running on Windows"
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if not hasattr(k32, "CreatePseudoConsole"):
            return ("this Windows build has no ConPTY — update to Windows 10 "
                    "1809 (build 17763) or newer")
    except Exception as e:                                  # pragma: no cover
        return f"kernel32 unavailable: {e}"
    return None


class ConPtyProcess:
    """A child process attached to a Windows pseudo-console."""

    def __init__(self, argv, cwd, env=None, cols=100, rows=30):
        reason = unsupported_reason()
        if reason:
            raise NotImplementedError(reason)

        import ctypes
        from ctypes import wintypes
        self._ctypes = ctypes
        self._wintypes = wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._k32 = k32

        class COORD(ctypes.Structure):
            _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

        class STARTUPINFOW(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
                ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
                ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                ("dwXCountChars", wintypes.DWORD),
                ("dwYCountChars", wintypes.DWORD),
                ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
                ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE),
                ("hStdError", wintypes.HANDLE)]

        class STARTUPINFOEXW(ctypes.Structure):
            _fields_ = [("StartupInfo", STARTUPINFOW),
                        ("lpAttributeList", ctypes.c_void_p)]

        class PROCESS_INFORMATION(ctypes.Structure):
            _fields_ = [("hProcess", wintypes.HANDLE),
                        ("hThread", wintypes.HANDLE),
                        ("dwProcessId", wintypes.DWORD),
                        ("dwThreadId", wintypes.DWORD)]

        self._COORD = COORD
        k32.CreatePseudoConsole.argtypes = [COORD, wintypes.HANDLE,
                                            wintypes.HANDLE, wintypes.DWORD,
                                            ctypes.POINTER(wintypes.HANDLE)]
        k32.CreatePseudoConsole.restype = ctypes.HRESULT
        k32.ResizePseudoConsole.argtypes = [wintypes.HANDLE, COORD]
        k32.ResizePseudoConsole.restype = ctypes.HRESULT
        k32.ClosePseudoConsole.argtypes = [wintypes.HANDLE]
        k32.ClosePseudoConsole.restype = None

        # two pipes: one feeds the console's input, one drains its output
        in_read = wintypes.HANDLE()
        in_write = wintypes.HANDLE()
        out_read = wintypes.HANDLE()
        out_write = wintypes.HANDLE()
        if not k32.CreatePipe(ctypes.byref(in_read), ctypes.byref(in_write),
                              None, 0):
            raise OSError("CreatePipe (input) failed")
        if not k32.CreatePipe(ctypes.byref(out_read), ctypes.byref(out_write),
                              None, 0):
            raise OSError("CreatePipe (output) failed")

        hpc = wintypes.HANDLE()
        hr = k32.CreatePseudoConsole(COORD(max(2, cols), max(2, rows)),
                                     in_read, out_write, 0, ctypes.byref(hpc))
        if hr != _S_OK:
            raise OSError(f"CreatePseudoConsole failed (HRESULT 0x{hr & 0xFFFFFFFF:08x})")
        # the console owns these ends now
        k32.CloseHandle(in_read)
        k32.CloseHandle(out_write)

        # STARTUPINFOEX carrying the pseudo-console attribute
        size = ctypes.c_size_t(0)
        k32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        buf = ctypes.create_string_buffer(size.value)
        si_ex = STARTUPINFOEXW()
        si_ex.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
        si_ex.lpAttributeList = ctypes.cast(buf, ctypes.c_void_p)
        if not k32.InitializeProcThreadAttributeList(si_ex.lpAttributeList, 1, 0,
                                                     ctypes.byref(size)):
            raise OSError("InitializeProcThreadAttributeList failed")
        if not k32.UpdateProcThreadAttribute(
                si_ex.lpAttributeList, 0, _PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
                hpc, ctypes.sizeof(wintypes.HANDLE), None, None):
            raise OSError("UpdateProcThreadAttribute failed")

        full_env = dict(os.environ)
        full_env.update(env or {})
        env_block = build_environment_block(full_env)
        cmdline = ctypes.create_unicode_buffer(build_command_line(argv))
        pi = PROCESS_INFORMATION()
        ok = k32.CreateProcessW(
            None, cmdline, None, None, False,
            _EXTENDED_STARTUPINFO_PRESENT | _CREATE_UNICODE_ENVIRONMENT,
            ctypes.create_string_buffer(env_block), cwd or None,
            ctypes.byref(si_ex.StartupInfo), ctypes.byref(pi))
        if not ok:
            err = ctypes.get_last_error()
            k32.ClosePseudoConsole(hpc)
            raise OSError(f"CreateProcessW failed (error {err}): {argv[0]}")

        k32.DeleteProcThreadAttributeList(si_ex.lpAttributeList)
        k32.CloseHandle(pi.hThread)

        self._hpc = hpc
        self._in_write = in_write
        self._out_read = out_read
        self._hprocess = pi.hProcess
        self.pid = pi.dwProcessId
        self._closed = False

    # ---------------------------------------------------------------- io

    def read(self, n=65536):
        """Blocking read of console output; b'' when the console closes."""
        ctypes = self._ctypes
        buf = ctypes.create_string_buffer(n)
        got = self._wintypes.DWORD(0)
        ok = self._k32.ReadFile(self._out_read, buf, n, ctypes.byref(got), None)
        if not ok or got.value == 0:
            return b""
        return buf.raw[:got.value]

    def write(self, data: bytes):
        ctypes = self._ctypes
        written = self._wintypes.DWORD(0)
        self._k32.WriteFile(self._in_write, data, len(data),
                            ctypes.byref(written), None)

    def set_size(self, cols, rows):
        try:
            self._k32.ResizePseudoConsole(
                self._hpc, self._COORD(max(2, cols), max(2, rows)))
        except OSError:
            pass

    # ---------------------------------------------------------------- lifecycle

    def alive(self):
        ctypes = self._ctypes
        code = self._wintypes.DWORD(0)
        if not self._k32.GetExitCodeProcess(self._hprocess, ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE

    def send_ctrl_c(self):
        """Closest analogue to an interrupt: ETX on the console input."""
        self.write(b"\x03")

    def request_close(self):
        """Closing the pseudo-console delivers CTRL_CLOSE_EVENT to the child,
        which is Windows' equivalent of a hangup: cleanup handlers (and thus
        Claude Code's SessionEnd hooks) get a chance to run."""
        if not self._closed:
            self._closed = True
            try:
                self._k32.ClosePseudoConsole(self._hpc)
            except OSError:
                pass

    def terminate(self):
        try:
            self._k32.TerminateProcess(self._hprocess, 1)
        except OSError:
            pass

    def close_handles(self):
        for h in (self._in_write, self._out_read, self._hprocess):
            try:
                self._k32.CloseHandle(h)
            except OSError:
                pass
