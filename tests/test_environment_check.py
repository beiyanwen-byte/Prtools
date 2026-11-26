"""Offline environment-check tests."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import environment_check


class EnvironmentCheckTests(unittest.TestCase):
    def test_complete_environment_is_ok(self):
        with patch.object(environment_check, "_tk_available", return_value=True), \
                patch.object(environment_check, "_git_available", return_value=True), \
                patch.object(environment_check, "_ssh_available", return_value=True), \
                patch.object(environment_check.EnvironmentCheck, "platform_name", return_value="linux"):
            result = environment_check.run_environment_check()
        self.assertTrue(result["ok"])
        self.assertEqual([], result["failures"])

    def test_missing_tk_is_described_with_linux_hint(self):
        with patch.object(environment_check, "_tk_available", return_value=False), \
                patch.object(environment_check, "_git_available", return_value=True), \
                patch.object(environment_check, "_ssh_available", return_value=True), \
                patch.object(environment_check.EnvironmentCheck, "platform_name", return_value="linux"):
            result = environment_check.run_environment_check()
        self.assertFalse(result["ok"])
        self.assertTrue(result["python_ok"])
        self.assertEqual("tk_missing", result["failures"][0]["code"])
        self.assertIn("python3-tk", result["failures"][0]["hint"])

    @unittest.skipIf(os.name == "nt", "Windows reports startup errors in a message box")
    def test_gui_reports_missing_tk_before_creating_a_window(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "tkinter.py").write_text('raise ModuleNotFoundError("no tkinter")\n', encoding="utf-8")
            env = os.environ.copy()
            env["PYTHONPATH"] = temp
            result = subprocess.run([sys.executable, "-B", str(root / "app" / "desktop_gui.py")],
                                    env=env, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Tkinter is unavailable", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_macos_hint_is_actionable(self):
        with patch.object(environment_check, "_tk_available", return_value=False), \
                patch.object(environment_check, "_git_available", return_value=True), \
                patch.object(environment_check, "_ssh_available", return_value=True), \
                patch.object(environment_check.EnvironmentCheck, "platform_name", return_value="macos"):
            result = environment_check.run_environment_check()
        self.assertFalse(result["ok"])
        self.assertIn("python.org", result["failures"][0]["hint"])

    def test_missing_git_is_actionable_and_sensitive_values_are_not_included(self):
        with patch.object(environment_check, "_tk_available", return_value=True), \
                patch.object(environment_check, "_git_available", return_value=False), \
                patch.object(environment_check, "_ssh_available", return_value=True), \
                patch.object(environment_check.EnvironmentCheck, "platform_name", return_value="macos"):
            result = environment_check.run_environment_check()
        serialized = repr(result)
        self.assertFalse(result["ok"])
        self.assertEqual("git_missing", result["failures"][0]["code"])
        self.assertNotIn("/Users/", serialized)
        self.assertNotIn("ghp_", serialized)


if __name__ == "__main__":
    unittest.main()
