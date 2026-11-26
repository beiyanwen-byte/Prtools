"""Checks for common accidental privacy leaks in tracked text files.

This is a heuristic, not a guarantee. It deliberately does not read ignored
local settings, credential stores or Git internals.
"""

import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHECKS = {
    "machine-specific home path": re.compile(
        r"(?i)(?:/home/(?!user(?:/|\b)|example\b|synthetic\b)[A-Za-z0-9_.-]+/"
        r"|/Users/(?!user\b|example\b)[A-Za-z0-9_.-]+/"
        r"|[A-Z]:[\\/]{1,2}Users[\\/]{1,2}(?!user\b|example\b)[A-Za-z0-9_.-]+)"
    ),
    "non-example email": re.compile(
        r"(?i)\b[A-Z0-9._%+-]+@(?!example\.(?:com|org|net|invalid)\b"
        r"|localhost\b|github\.com\b|test\.invalid\b)[A-Z0-9.-]+\.[A-Z]{2,}\b"
    ),
    "token or private-key-like text": re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"
        r"|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    ),
}


class TrackedPrivacyTest(unittest.TestCase):
    def test_tracked_files_do_not_contain_common_private_data_patterns(self):
        try:
            result = subprocess.run(["git", "ls-files", "-z", "--cached"], cwd=ROOT,
                                    capture_output=True, timeout=15, check=False)
        except (OSError, subprocess.TimeoutExpired):
            self.skipTest("Git workspace is unavailable")
        if result.returncode:
            self.skipTest("Git workspace is unavailable")
        findings = []
        for raw in result.stdout.split(b"\0"):
            if not raw:
                continue
            relative = raw.decode("utf-8", errors="surrogateescape")
            if relative in (".preferences.json", ".github-app.json") or relative.startswith(".github-pr-tool-runtime/"):
                findings.append((relative, "local secret or preferences file is tracked"))
                continue
            path = ROOT / relative
            try:
                if path.is_symlink():
                    text = str(path.readlink())
                elif path.is_file() and path.stat().st_size <= 5_000_000:
                    text = path.read_text(encoding="utf-8")
                else:
                    continue
            except (OSError, UnicodeError):
                continue
            for label, pattern in CHECKS.items():
                if pattern.search(text):
                    findings.append((relative, label))
        self.assertEqual([], findings, "Remove private data from tracked files; values are not printed.")


if __name__ == "__main__":
    unittest.main()
