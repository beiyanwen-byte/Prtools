"""Pre-launch environment checks for the desktop application."""

from __future__ import annotations

import os
import shutil
import sys


class EnvironmentCheck:
    @staticmethod
    def platform_name():
        if os.name == "nt":
            return "windows"
        environ = os.environ
        if environ.get("WSL_DISTRO_NAME"):
            return "wsl"
        return "macos" if sys_platform_name() == "darwin" else "linux"


def sys_platform_name():
    import sys
    return sys.platform


def _python_version():
    import sys
    return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"


def _git_available():
    return shutil.which("git") is not None


def _ssh_available():
    return shutil.which("ssh") is not None


def _tk_available():
    try:
        import tkinter  # noqa: F401
        return True
    except (ImportError, RuntimeError, OSError):
        return False


def tk_install_hint(platform_name):
    if platform_name == "windows":
        return "Enable Tcl/Tk when installing Python from python.org."
    if platform_name == "macos":
        return "Install Python 3 with Tk support, or use the official python.org installer."
    if platform_name in ("linux", "wsl"):
        return ("On Debian/Ubuntu, run: sudo apt install python3-tk\n"
                "For a custom Python, install or use a matching Python with Tk support.")
    return "Install Tkinter/Tcl for Python."


def report_tk_startup_error():
    """Report missing Tk before the GUI is available, including under pythonw."""
    message = "Tkinter is unavailable for this Python.\n" + tk_install_hint(EnvironmentCheck.platform_name())
    if sys.stderr is not None:
        print(message, file=sys.stderr)
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, "GitHub PR Desk", 0x10)
        except (AttributeError, OSError):
            pass


def run_environment_check():
    """Return fixed safe checks and install hints, never paths or credentials."""
    failures = []
    platform_name = EnvironmentCheck.platform_name()
    python_ok = True  # This check is running in Python already.
    tk_ok = _tk_available()
    git_ok = _git_available()
    ssh_ok = _ssh_available()

    if not tk_ok:
        failures.append({"code": "tk_missing", "message": "Tkinter is unavailable.",
                         "hint": tk_install_hint(platform_name)})
    if not git_ok:
        failures.append({"code": "git_missing", "message": "Git is unavailable.",
                         "hint": "Install Git and make it available on PATH."})
    if platform_name in ("linux", "wsl", "macos") and not ssh_ok:
        failures.append({"code": "ssh_missing", "message": "OpenSSH is unavailable.",
                         "hint": "Install and configure OpenSSH for SSH remotes."})

    import sys
    return {"platform": platform_name, "python": _python_version(),
            "python_ok": python_ok, "tk_ok": tk_ok, "git_ok": git_ok,
            "ssh_ok": ssh_ok, "wsl": platform_name == "wsl",
            "ok": not failures, "failures": failures,
            "python_executable_available": bool(sys.executable)}
