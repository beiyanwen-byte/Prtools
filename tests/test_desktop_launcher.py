"""Windows cmd smoke tests for the packaged desktop launcher (offline stubs only)."""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STUB_SOURCE = r"""
using System;
using System.IO;
using System.Text;
public class LauncherStub {
    public static int Main(string[] args) {
        string name = Path.GetFileNameWithoutExtension(
            System.Diagnostics.Process.GetCurrentProcess().MainModule.FileName);
        using (var file = new StreamWriter(Environment.GetEnvironmentVariable("SMOKE_ARGS"), true)) {
            file.WriteLine(name);
            foreach (string arg in args)
                file.WriteLine(Convert.ToBase64String(Encoding.UTF8.GetBytes(arg)));
            file.WriteLine("-");
        }
        return 0;
    }
}
"""


@unittest.skipUnless(os.name == "nt", "requires Windows cmd.exe and PowerShell")
class DesktopLauncherTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="desktop-launcher-stubs ")
        cls.directory = Path(cls.temp.name)
        source = cls.directory / "stub.cs"
        source.write_text(STUB_SOURCE, encoding="utf-8")
        env = os.environ.copy()
        env["SMOKE_STUB_SOURCE"] = str(source)
        env["SMOKE_STUB_EXE"] = str(cls.directory / "launcher_stub.exe")
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             "Add-Type -Path $env:SMOKE_STUB_SOURCE -OutputAssembly $env:SMOKE_STUB_EXE -OutputType ConsoleApplication"],
            env=env, capture_output=True, text=True, timeout=30, check=False,
        )
        if result.returncode:
            cls.temp.cleanup()
            raise RuntimeError("Cannot compile offline launcher stub")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def _run_launcher(self, adapter, stub_names):
        log = self.directory / "arguments.txt"
        log.unlink(missing_ok=True)
        for name in ("pyw", "pythonw"):
            (self.directory / (name + ".exe")).unlink(missing_ok=True)
        for name in stub_names:
            shutil.copyfile(self.directory / "launcher_stub.exe", self.directory / (name + ".exe"))
        env = os.environ.copy()
        env["SMOKE_ARGS"] = str(log)
        system32 = os.path.join(os.environ["SystemRoot"], "System32")
        env["PATH"] = os.pathsep.join((str(self.directory), system32,
                                      os.path.join(system32, "WindowsPowerShell", "v1.0")))
        result = subprocess.run(
            '"' + os.environ["COMSPEC"] + '" /d /c call "' + str(adapter) + '"',
            cwd=self.directory, env=env, capture_output=True, text=True, timeout=30, check=False,
        )
        deadline = time.monotonic() + 3
        while not log.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        calls = []
        if log.exists():
            lines = iter(log.read_text(encoding="utf-8").splitlines())
            for name in lines:
                args = []
                for line in lines:
                    if line == "-":
                        break
                    args.append(base64.b64decode(line).decode("utf-8"))
                calls.append((name.lower(), args))
        return result, calls

    def _copy_package(self, directory):
        shutil.copytree(ROOT / "app", Path(directory) / "app",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        adapter = Path(directory) / "start-desktop.cmd"
        shutil.copyfile(ROOT / "start-desktop.cmd", adapter)
        return adapter

    def test_native_gui_launcher_quotes_paths_and_falls_back_to_pythonw(self):
        with tempfile.TemporaryDirectory(prefix="package with spaces ") as temp:
            adapter = self._copy_package(temp)
            cases = (("pyw", ("pyw", "pythonw"), ["-3", str(Path(temp) / "app" / "desktop_gui.py")]),
                     ("pythonw", ("pythonw",), [str(Path(temp) / "app" / "desktop_gui.py")]))
            for selected, stubs, expected in cases:
                with self.subTest(selected=selected):
                    result, calls = self._run_launcher(adapter, stubs)
                    self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                    self.assertEqual([(selected, expected)], calls)

    def test_unc_checkout_still_starts_windows_gui_from_package(self):
        match = re.match(r"^\\\\wsl(?:\.localhost|\$)\\([^\\]+)\\", str(ROOT), re.I)
        if not match:
            self.skipTest("package must be on a WSL UNC share")
        with tempfile.TemporaryDirectory(prefix="unc package with spaces ", dir=ROOT.parent) as temp:
            adapter = self._copy_package(temp)
            result, calls = self._run_launcher(adapter, ("pyw", "pythonw"))
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual([("pyw", ["-3", str(Path(temp) / "app" / "desktop_gui.py")])], calls)


if __name__ == "__main__":
    unittest.main()
