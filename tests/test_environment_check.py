"""Offline environment-check tests."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import environment_check


class EnvironmentCheckTests(unittest.TestCase):
    def test_complete_environment_is_ok(self):
        with patch.object(environment_check.importlib.util, "find_spec", return_value=object()), \
                patch.object(environment_check, "_tk_available", return_value=True), \
                patch.object(environment_check, "_git_available", return_value=True), \
                patch.object(environment_check, "_ssh_available", return_value=True), \
                patch.object(environment_check.EnvironmentCheck, "platform_name", return_value="linux"):
            result = environment_check.run_environment_check()
        self.assertTrue(result["ok"])
        self.assertEqual([], result["failures"])

    def test_missing_tk_is_described_with_linux_hint(self):
        with patch.object(environment_check.importlib.util, "find_spec", return_value=object()), \
                patch.object(environment_check, "_tk_available", return_value=False), \
                patch.object(environment_check, "_git_available", return_value=True), \
                patch.object(environment_check, "_ssh_available", return_value=True), \
                patch.object(environment_check.EnvironmentCheck, "platform_name", return_value="linux"):
            result = environment_check.run_environment_check()
        self.assertFalse(result["ok"])
        self.assertEqual("tk_missing", result["failures"][0]["code"])
        self.assertIn("python3-tk", result["failures"][0]["hint"])

    def test_macos_hint_is_actionable(self):
        with patch.object(environment_check.importlib.util, "find_spec", return_value=object()), \
                patch.object(environment_check, "_tk_available", return_value=False), \
                patch.object(environment_check, "_git_available", return_value=True), \
                patch.object(environment_check, "_ssh_available", return_value=True), \
                patch.object(environment_check.EnvironmentCheck, "platform_name", return_value="macos"):
            result = environment_check.run_environment_check()
        self.assertFalse(result["ok"])
        self.assertIn("python.org", result["failures"][0]["hint"])

    def test_missing_git_is_actionable_and_sensitive_values_are_not_included(self):
        with patch.object(environment_check.importlib.util, "find_spec", return_value=object()), \
                patch.object(environment_check, "_tk_available", return_value=True), \
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
