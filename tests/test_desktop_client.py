"""Offline tests for Windows-to-WSL backend routing and workspace paths."""

import io
import json
import os
import subprocess
import unittest
from unittest.mock import Mock, patch

import desktop_client
from desktop_backend import DesktopBackendError


class DesktopClientTests(unittest.TestCase):
    def test_windows_installation_path_is_resolved_inside_selected_wsl(self):
        completed = Mock(returncode=0, stdout="/mnt/c/example folder/app/desktop_worker.py\n")
        path = r"C:\example folder\app\desktop_worker.py"
        with patch.object(desktop_client.subprocess, "run", return_value=completed) as run:
            result = desktop_client._windows_path_in_wsl(path, "ExampleDistro")
        self.assertEqual("/mnt/c/example folder/app/desktop_worker.py", result)
        self.assertEqual(["wsl.exe", "-d", "ExampleDistro", "--exec", "wslpath", "-u", path],
                         run.call_args.args[0])

    @unittest.skipUnless(os.name == "nt", "requires Windows path semantics")
    def test_windows_installation_routes_wsl_checkout_to_same_distribution(self):
        client = desktop_client.DesktopBackendClient()
        selected = r"\\wsl.localhost\ExampleDistro\home\example\checkout"
        def locate(path):
            return ("ExampleDistro", "/home/example/checkout") if str(path) == selected else None
        with patch.object(desktop_client, "_wsl_path", side_effect=locate), \
                patch.object(desktop_client, "_windows_path_in_wsl",
                             return_value="/mnt/c/example/app/desktop_worker.py") as convert:
            command, mode, repo_path = client._command(selected)
        self.assertEqual(("wsl", "ExampleDistro"), mode)
        self.assertEqual("/home/example/checkout", repo_path)
        self.assertEqual(["wsl.exe", "-d", "ExampleDistro", "--exec", "python3", "-u",
                          "/mnt/c/example/app/desktop_worker.py"], command)
        convert.assert_called_once()

    def test_wsl_path_conversion_failure_does_not_expose_raw_output(self):
        with patch.object(desktop_client.subprocess, "run", return_value=Mock(
                returncode=1, stdout="private diagnostic")):
            with self.assertRaises(DesktopBackendError) as caught:
                desktop_client._windows_path_in_wsl(r"C:\example\worker.py", "ExampleDistro")
        self.assertEqual("wsl_backend_unavailable", caught.exception.code)
        self.assertNotIn("private diagnostic", caught.exception.message)
        with patch.object(desktop_client.subprocess, "run", side_effect=subprocess.TimeoutExpired("wsl", 15)):
            with self.assertRaises(DesktopBackendError):
                desktop_client._windows_path_in_wsl(r"C:\example\worker.py", "ExampleDistro")

    def test_workspace_path_remains_gui_visible_while_connect_path_is_normalized(self):
        client = desktop_client.DesktopBackendClient()
        original = r"\\wsl.localhost\ExampleDistro\home\example\workspace"
        process = Mock()
        process.poll.return_value = None
        process.stdin = io.StringIO()
        process.stdout = io.StringIO('{"ok":true,"data":{"saved":true}}\n')
        def start(_path):
            client._process = process
            client._mode = ("wsl", "ExampleDistro")
            client._wsl_repo_path = "/home/example/workspace"
        with patch.object(client, "_start", side_effect=start):
            client.remember_local_repository_path(original)
        request = json.loads(process.stdin.getvalue())
        self.assertEqual(original, request["path"])
        process.stdin = io.StringIO()
        process.stdout = io.StringIO('{"ok":true,"data":{}}\n')
        with patch.object(client, "_start", side_effect=start):
            client.connect_repository(original)
        request = json.loads(process.stdin.getvalue())
        self.assertEqual("/home/example/workspace", request["path"])

    def test_saved_settings_keep_gui_visible_workspace_path(self):
        client = desktop_client.DesktopBackendClient()
        client._mode = ("wsl", "ExampleDistro")
        path = r"\\wsl.localhost\ExampleDistro\home\example\workspace"
        with patch.object(client, "call", return_value={}) as call:
            client.save_settings({"local_repository_path": path})
        self.assertEqual(path, call.call_args.kwargs["draft"]["local_repository_path"])


if __name__ == "__main__":
    unittest.main()
