"""Offline tests: fake Windows credential API; no real secret storage writes."""

import ctypes
import unittest
from unittest.mock import patch

import windows_repository_credentials as credentials


class FakeCredentialAPI:
    def __init__(self):
        self.saved = None
        self.freed = False
        self._entry = None
        self._buffer = None

    def CredWriteW(self, ptr, flags):
        entry = ctypes.cast(ptr, ctypes.POINTER(credentials._CREDENTIALW)).contents
        self.saved = (entry.TargetName, entry.Type, entry.Persist,
                      ctypes.string_at(entry.CredentialBlob, entry.CredentialBlobSize).decode("utf-16-le"))
        return 1

    def CredReadW(self, target, _kind, _flags, out):
        if not self.saved or target != self.saved[0]:
            raise AssertionError("Unexpected credential lookup")
        blob = self.saved[3].encode("utf-16-le")
        self._buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        self._entry = credentials._CREDENTIALW()
        self._entry.CredentialBlobSize = len(blob)
        self._entry.CredentialBlob = ctypes.cast(self._buffer, ctypes.POINTER(ctypes.c_ubyte))
        pointer = ctypes.pointer(self._entry)
        ctypes.cast(out, ctypes.POINTER(ctypes.POINTER(credentials._CREDENTIALW)))[0] = pointer
        return 1

    def CredFree(self, _ptr):
        self.freed = True


class WindowsRepositoryCredentialTests(unittest.TestCase):
    def test_scoped_windows_credential_round_trip_with_fake_api(self):
        api = FakeCredentialAPI()
        with patch.object(credentials, "_api", return_value=api):
            credentials.save("Owner/Repo", "synthetic-token")
            found = credentials.read("owner/repo")
        self.assertEqual("GitHubPRDesk:github.com/owner/repo", api.saved[0])
        self.assertEqual((1, 2), api.saved[1:3])
        self.assertEqual("synthetic-token", found)
        self.assertTrue(api.freed)
        self.assertNotEqual(credentials._target("owner/other"), api.saved[0])

    def test_invalid_repository_and_token_are_not_stored(self):
        api = FakeCredentialAPI()
        with patch.object(credentials, "_api", return_value=api):
            for repository, token in (("owner/../bad", "synthetic"), ("owner/repo", "bad\ntoken")):
                with self.subTest(repository=repository), self.assertRaises(credentials.CredentialStorageError):
                    credentials.save(repository, token)
        self.assertIsNone(api.saved)


if __name__ == "__main__":
    unittest.main()
