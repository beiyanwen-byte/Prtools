"""Repository-scoped secrets in Windows Credential Manager, never project files."""

from __future__ import annotations

import ctypes
import os
import re
from ctypes import wintypes


class CredentialStorageError(Exception):
    pass


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def _target(repository):
    if not isinstance(repository, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,39}/[A-Za-z0-9_.-]{1,100}", repository):
        raise CredentialStorageError("Invalid repository identity.")
    return "GitHubPRDesk:github.com/" + repository.casefold()


def _api():
    if os.name != "nt":
        raise CredentialStorageError("Windows Credential Manager is unavailable.")
    try:
        api = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.POINTER(ctypes.POINTER(_CREDENTIALW))]
        api.CredReadW.restype = wintypes.BOOL
        api.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
        api.CredWriteW.restype = wintypes.BOOL
        api.CredFree.argtypes = [ctypes.c_void_p]
        return api
    except (OSError, AttributeError):
        raise CredentialStorageError("Windows Credential Manager is unavailable.") from None


def read(repository):
    api = _api()
    entry = ctypes.POINTER(_CREDENTIALW)()
    if not api.CredReadW(_target(repository), 1, 0, ctypes.byref(entry)):
        if ctypes.get_last_error() == 1168:  # ERROR_NOT_FOUND
            return None
        raise CredentialStorageError("Windows Credential Manager could not read this credential.")
    try:
        size = entry.contents.CredentialBlobSize
        if not 0 < size <= 16384 or size % 2:
            raise CredentialStorageError("The saved credential is invalid.")
        token = ctypes.string_at(entry.contents.CredentialBlob, size).decode("utf-16-le")
        if not re.fullmatch(r"[!-~]{1,8192}", token):
            raise CredentialStorageError("The saved credential is invalid.")
        return token
    except UnicodeError:
        raise CredentialStorageError("The saved credential is invalid.") from None
    finally:
        api.CredFree(entry)


def save(repository, token):
    if not isinstance(token, str) or not re.fullmatch(r"[!-~]{1,8192}", token):
        raise CredentialStorageError("The credential is invalid.")
    api = _api()
    data = token.encode("utf-16-le")
    if len(data) > 2560:  # CRED_MAX_CREDENTIAL_BLOB_SIZE
        raise CredentialStorageError("The credential is too large for Windows Credential Manager.")
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    entry = _CREDENTIALW()
    entry.Type = 1  # CRED_TYPE_GENERIC
    entry.TargetName = _target(repository)
    entry.CredentialBlobSize = len(data)
    entry.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
    entry.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE
    entry.UserName = "GitHub token"
    try:
        if not api.CredWriteW(ctypes.byref(entry), 0):
            raise CredentialStorageError("Windows Credential Manager could not save this credential.")
    finally:
        ctypes.memset(buffer, 0, len(data))
