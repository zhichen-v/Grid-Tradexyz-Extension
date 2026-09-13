"""A hidden Windows console and a kill-on-close job for launcher contract tests."""

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys
import threading
import time


_CREATE_LOCK = threading.Lock()


class _StartupInfo(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
        *[(name, wintypes.DWORD) for name in ("dwX", "dwY", "dwXSize", "dwYSize",
            "dwXCountChars", "dwYCountChars", "dwFillAttribute", "dwFlags")],
        ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]


class _ProcessInfo(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]


class _JobLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong), ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in ("ReadOperationCount", "WriteOperationCount",
        "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _ExtendedJobLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _JobLimits), ("IoInfo", _IoCounters),
        *[(name, ctypes.c_size_t) for name in ("ProcessMemoryLimit", "JobMemoryLimit",
            "PeakProcessMemoryUsed", "PeakJobMemoryUsed")]]


class _JobAccounting(ctypes.Structure):
    _fields_ = [(name, ctypes.c_longlong) for name in ("TotalUserTime", "TotalKernelTime",
        "ThisPeriodTotalUserTime", "ThisPeriodTotalKernelTime")] + [
        (name, wintypes.DWORD) for name in ("TotalPageFaultCount", "TotalProcesses",
            "ActiveProcesses", "TotalTerminatedProcesses")]


def _kernel():
    if os.name != "nt":
        raise RuntimeError("Windows console tests require Windows")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
        "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
        "QueryInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
        "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        "TerminateProcess": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        "CreateProcessW": ([wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
            wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
            ctypes.POINTER(_StartupInfo), ctypes.POINTER(_ProcessInfo)], wintypes.BOOL),
        "SetHandleInformation": ([wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD], wintypes.BOOL),
        "ResumeThread": ([wintypes.HANDLE], wintypes.DWORD),
        "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
        "GetExitCodeProcess": ([wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        "FreeConsole": ([], wintypes.BOOL),
        "AttachConsole": ([wintypes.DWORD], wintypes.BOOL),
        "SetConsoleCtrlHandler": ([ctypes.c_void_p, wintypes.BOOL], wintypes.BOOL),
        "GenerateConsoleCtrlEvent": ([wintypes.DWORD, wintypes.DWORD], wintypes.BOOL),
    }
    for name, (args, result) in signatures.items():
        method = getattr(kernel, name)
        method.argtypes, method.restype = args, result
    return kernel


def _check(success):
    if not success:
        raise ctypes.WinError(ctypes.get_last_error())


def clean_environment(profile_root=None):
    """Only OS/runtime variables; never inherit account, proxy or Python hooks."""
    names = {"SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP",
             "LOCALAPPDATA", "APPDATA", "USERPROFILE", "PROGRAMDATA"}
    values = {key: value for key, value in os.environ.items() if key.upper() in names}
    values.update(PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1",
                  PATHEXT=".COM;.EXE;.BAT;.CMD")
    values["PATH"] = os.pathsep.join((str(Path(sys.executable).parent),
        str(Path(os.environ["SystemRoot"]) / "System32"),
        str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0")))
    if profile_root is not None:
        profile = Path(profile_root).resolve()
        for key, path in {"USERPROFILE": profile, "APPDATA": profile / "Roaming",
                          "LOCALAPPDATA": profile / "Local", "PROGRAMDATA": profile / "ProgramData",
                          "TEMP": profile / "Temp", "TMP": profile / "Temp"}.items():
            path.mkdir(parents=True, exist_ok=True)
            values[key] = str(path)
    return values


class WindowsConsoleProcess:
    """Start suspended, assign the entire process tree to a job, then resume."""

    def __init__(self, command, *, cwd, env, stdout, stderr):
        import msvcrt
        self.kernel = _kernel()
        self.job = self.process = None
        self.pid = None
        self.command = list(map(str, command))
        self.env = dict(env)
        self.watchdog_used = False
        self.job = self.kernel.CreateJobObjectW(None, None)
        _check(self.job)
        limits = _ExtendedJobLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        info = _ProcessInfo()
        try:
            _check(self.kernel.SetInformationJobObject(self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
            # Concurrent cases cannot inherit each other's temporary stdio handles.
            with _CREATE_LOCK, open(os.devnull, "rb") as input_file, open(stdout, "wb") as output_file, open(stderr, "wb") as error_file:
                handles = [msvcrt.get_osfhandle(stream.fileno()) for stream in (input_file, output_file, error_file)]
                for handle in handles:
                    _check(self.kernel.SetHandleInformation(handle, 1, 1))
                startup = _StartupInfo()
                startup.cb = ctypes.sizeof(startup)
                startup.dwFlags, startup.wShowWindow = 0x101, 0  # STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW, hidden
                startup.hStdInput, startup.hStdOutput, startup.hStdError = handles
                environment = ctypes.create_unicode_buffer("\0".join(f"{key}={value}" for key, value in sorted(env.items())) + "\0")
                line = ctypes.create_unicode_buffer(subprocess.list2cmdline(self.command))
                _check(self.kernel.CreateProcessW(self.command[0], line, None, None, True,
                    0x10 | 0x4 | 0x400, environment, str(cwd), ctypes.byref(startup), ctypes.byref(info)))
                self.process, self.pid = info.hProcess, info.dwProcessId
                _check(self.kernel.AssignProcessToJobObject(self.job, self.process))
                if self.kernel.ResumeThread(info.hThread) == 0xffffffff:
                    _check(False)
        except BaseException:
            if info.hProcess and not self.process:
                self.process = info.hProcess
            if self.process:
                self.kernel.TerminateProcess(self.process, 124)
            self.close()
            raise
        finally:
            if info.hThread:
                self.kernel.CloseHandle(info.hThread)

    def poll(self):
        waited = self.kernel.WaitForSingleObject(self.process, 0)
        if waited == 0xffffffff:
            _check(False)
        if waited == 258:
            return None
        result = wintypes.DWORD()
        _check(self.kernel.GetExitCodeProcess(self.process, ctypes.byref(result)))
        return result.value

    def wait(self, timeout):
        deadline = time.monotonic() + timeout
        waited = self.kernel.WaitForSingleObject(self.process, max(0, int(timeout * 1000)))
        if waited == 0xffffffff:
            _check(False)
        # The parent signal can precede the job accounting update (or a child
        # finishing). Natural completion requires the whole owned tree to drain.
        while waited != 258 and self.active_processes():
            if time.monotonic() >= deadline:
                waited = 258
                break
            time.sleep(0.01)
        if waited == 258:
            self.watchdog_used = True
            _check(self.kernel.TerminateJobObject(self.job, 124))
            self.kernel.WaitForSingleObject(self.process, 5000)
            raise subprocess.TimeoutExpired(self.command, timeout)
        return self.poll()

    def active_processes(self):
        counters = _JobAccounting()
        _check(self.kernel.QueryInformationJobObject(self.job, 1, ctypes.byref(counters), ctypes.sizeof(counters), None))
        return counters.ActiveProcesses

    def send_ctrl_c(self):
        # A separate helper joins only this hidden console. Never broadcast into
        # the user's console or substitute forceful process termination for Ctrl+C.
        if self.poll() is not None:
            raise RuntimeError("test process has already exited")
        result = subprocess.run([sys.executable, "-I", str(Path(__file__).resolve()), "--ctrl-c", str(self.pid)],
            env=self.env, capture_output=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise RuntimeError("isolated console Ctrl+C delivery failed")

    def close(self):
        if self.job:
            self.kernel.CloseHandle(self.job)
            self.job = None
        if self.process:
            self.kernel.WaitForSingleObject(self.process, 5000)
            self.kernel.CloseHandle(self.process)
            self.process = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _control(pid):
    if type(pid) is not int or not 0 < pid < 0xffffffff:
        raise ValueError("an exact test console process id is required")
    kernel = _kernel()
    kernel.FreeConsole()
    _check(kernel.AttachConsole(pid))
    try:
        _check(kernel.SetConsoleCtrlHandler(None, True))
        _check(kernel.GenerateConsoleCtrlEvent(0, 0))  # CTRL_C_EVENT, this isolated console
        time.sleep(0.1)
    finally:
        kernel.FreeConsole()


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--ctrl-c":
        raise SystemExit(2)
    try:
        _control(int(sys.argv[2]))
    except Exception:
        raise SystemExit(1) from None
