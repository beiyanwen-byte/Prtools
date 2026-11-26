"""GUI-side JSON IPC client for the credential-owning desktop worker."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

from desktop_backend import DesktopBackendError


_WSL_PATH_RE = re.compile(r"^//wsl(?:\.localhost|\$)/([^/]+)(/.*)?$", re.I)
MAX_RESPONSE_CHARS = 2 * 1024 * 1024


def _wsl_path(path):
    match = _WSL_PATH_RE.fullmatch(str(path).replace("\\", "/"))
    return (match.group(1), match.group(2) or "/") if match else None


def _windows_path_in_wsl(path, distro):
    """Resolve an installed Windows file through the selected WSL distribution."""
    try:
        result = subprocess.run(
            ["wsl.exe", "-d", distro, "--exec", "wslpath", "-u", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise DesktopBackendError("wsl_backend_unavailable", "Could not locate the application in the selected WSL distribution.") from None
    converted = result.stdout.rstrip("\r\n")
    if result.returncode or not converted.startswith("/") or "\n" in converted or "\r" in converted or "\x00" in converted:
        raise DesktopBackendError("wsl_backend_unavailable", "The application directory is not accessible from the selected WSL distribution.")
    return converted


class DesktopBackendClient:
    """Keep the token inside a worker process; the Tk UI receives safe data only."""

    def __init__(self):
        self._process = None
        self._mode = None
        self._lock = threading.Lock()
        self._wsl_repo_path = None

    def _command(self, repository_path=None):
        worker_path = Path(__file__).with_name("desktop_worker.py")
        if os.name == "nt":
            repo_location = _wsl_path(repository_path) if repository_path else None
            worker_location = _wsl_path(worker_path)
            if repo_location:
                distro, repo_linux_path = repo_location
                if worker_location:
                    if worker_location[0].casefold() != distro.casefold():
                        raise DesktopBackendError(
                            "wsl_backend_unavailable",
                            "For a WSL-hosted installation, select a repository in the same WSL distribution.",
                        )
                    _, worker_linux_path = worker_location
                else:
                    worker_linux_path = _windows_path_in_wsl(worker_path, distro)
                return (["wsl.exe", "-d", distro, "--exec", "python3", "-u", worker_linux_path],
                        ("wsl", distro), repo_linux_path)
            if not repository_path and worker_location:
                distro, worker_linux_path = worker_location
                return (["wsl.exe", "-d", distro, "--exec", "python3", "-u", worker_linux_path],
                        ("wsl", distro), None)
            return ([sys.executable, "-u", str(worker_path)], ("windows",), repository_path)
        return ([sys.executable, "-u", str(worker_path)], ("local",), repository_path)

    def _start(self, repository_path=None):
        command, mode, worker_repo_path = self._command(repository_path)
        same_mode = (self._mode == mode or (
            self._mode is not None and mode[0] == self._mode[0] == "wsl"
            and mode[1].casefold() == self._mode[1].casefold()))
        if self._process is not None and self._process.poll() is None and same_mode:
            self._wsl_repo_path = worker_repo_path or self._wsl_repo_path
            return
        self.close()
        try:
            self._process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                bufsize=1,
            )
        except OSError:
            self._process = None
            raise DesktopBackendError("backend_unavailable", "The desktop backend could not be started.") from None
        self._mode = mode
        self._wsl_repo_path = worker_repo_path

    def call(self, action, **fields):
        with self._lock:
            if action in ("describe_repository", "connect_repository", "remember_local_repository_path"):
                path = fields.get("path", "")
                self._start(path)
                if self._mode and self._mode[0] == "wsl" and action != "remember_local_repository_path":
                    fields["path"] = self._wsl_repo_path
            else:
                # The worker is session state: do not infer a new execution
                # environment from the normalized Linux path stored after a
                # Windows-to-WSL launch.
                if self._process is None or self._process.poll() is not None:
                    self._start()
            process = self._process
            if process is None or process.poll() is not None:
                raise DesktopBackendError("backend_unavailable", "The desktop backend is not running.")
            request = {"action": action, **fields}
            try:
                process.stdin.write(json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n")
                process.stdin.flush()
                line = process.stdout.readline(MAX_RESPONSE_CHARS + 1)
            except (OSError, ValueError):
                self.close()
                raise DesktopBackendError("backend_unavailable", "The desktop backend stopped unexpectedly.") from None
            if not line or len(line) > MAX_RESPONSE_CHARS:
                self.close()
                raise DesktopBackendError("backend_unavailable", "The desktop backend returned no usable response.")
            try:
                response = json.loads(line)
            except (ValueError, RecursionError):
                self.close()
                raise DesktopBackendError("backend_unavailable", "The desktop backend returned an invalid response.") from None
            if not isinstance(response, dict) or response.get("ok") is not True:
                error = response.get("error") if isinstance(response, dict) else None
                if not isinstance(error, dict):
                    raise DesktopBackendError("backend_unavailable", "The desktop backend operation failed.")
                raise DesktopBackendError(
                    error.get("code", "unknown_error"),
                    error.get("message", "The desktop backend operation failed."),
                )
            return response.get("data")

    def describe_repository(self, path):
        return self.call("describe_repository", path=path)

    def connect_repository(self, path, *, token=None, expected_repository=None):
        return self.call("connect_repository", path=path, token=token,
                         expected_repository=expected_repository)

    def load_branches(self):
        return self.call("load_branches")["branches"]

    def fetch_remote(self):
        return self.call("fetch_remote")

    def list_branch_tags(self):
        return self.call("list_branch_tags")

    def list_workflow_tag_runs(self, *, reset=False):
        return self.call("list_workflow_tag_runs", reset=reset)

    def suggest_tag_name(self, branch):
        return self.call("suggest_tag_name", branch=branch)

    def create_remote_tag(self, branch, tag_name, *, confirmed=False):
        return self.call("create_remote_tag", branch=branch, tag_name=tag_name,
                         confirmed=confirmed)

    def activity(self):
        return self.call("activity")

    def compare(self, base, source):
        return self.call("compare", base=base, source=source)

    def source_commit_detail(self, sha):
        return self.call("source_commit_detail", sha=sha)

    def search_reviewers(self, query):
        return self.call("search_reviewers", query=query)

    def load_settings(self):
        result = self.call("load_settings")
        draft = result.get("draft") if isinstance(result, dict) else None
        path = draft.get("local_repository_path") if isinstance(draft, dict) else None
        if os.name == "nt" and self._mode and self._mode[0] == "wsl" and isinstance(path, str) and path.startswith("/"):
            distro = self._mode[1]
            draft["local_repository_path"] = f"\\\\wsl.localhost\\{distro}" + path.replace("/", "\\")
        return result

    def save_settings(self, draft):
        # Preserve the GUI-visible workspace path, even when the worker is in WSL.
        return self.call("save_settings", draft=dict(draft))

    def remember_local_repository_path(self, path):
        return self.call("remember_local_repository_path", path=path)

    def remember_branch_selection(self, base, source):
        return self.call("remember_branch_selection", base=base, source=source)

    def inspect(self, base, source):
        return self.call("inspect", base=base, source=source)

    def create(self, base, source, title, body, reviewers, *, confirmed=False):
        return self.call("create", base=base, source=source, title=title, body=body,
                         reviewers=reviewers, confirmed=confirmed)

    def sign_out(self):
        if self._process is not None and self._process.poll() is None:
            try:
                self.call("sign_out")
            except DesktopBackendError:
                pass

    def close(self):
        process, self._process = self._process, None
        self._mode = None
        self._wsl_repo_path = None
        if process is not None:
            try:
                if process.stdin:
                    process.stdin.close()
                process.terminate()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass
