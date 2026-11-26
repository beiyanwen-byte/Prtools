"""Pre-launch environment checks for the desktop application."""

from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import subprocess
from pathlib import Path


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
    except (ImportError, RuntimeError):
        return False


def run_environment_check():
    """Return fixed safe checks and install hints, never paths or credentials."""
    failures = []
    platform_name = EnvironmentCheck.platform_name()
    python_ok = importlib.util.find_spec("tkinter") is not None
    tk_ok = _tk_available()
    git_ok = _git_available()
    ssh_ok = _ssh_available()

    if not python_ok:
        failures.append({"code": "python_missing", "message": "Python 3 is unavailable.",
                         "hint": "Install Python 3 and add it to PATH."})
    if not tk_ok:
        hint = "Install Tkinter/Tcl for Python."
        if platform_name == "windows":
            hint = "Enable Tcl/Tk when installing Python from python.org."
        elif platform_name == "macos":
            hint = "Install Python 3 with Tk support, or use the official python.org installer."
        elif platform_name in ("linux", "wsl"):
            hint = "Install python3-tk (for example: apt install python3-tk)."
        failures.append({"code": "tk_missing", "message": "Tkinter is unavailable.", "hint": hint})
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
