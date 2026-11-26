"""Strict, local-only reader and writer for desktop preferences.

The store never reads environment variables, legacy app files, or repository
configuration. Callers should resolve the installation-local preferences path
with :func:`install_path`.
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from threading import Lock


_WRITE_LOCK = Lock()
SUPPORTED_FIELDS = {
    "repo", "base", "base_ref", "reviewers", "desktop_configured",
    "github_app_client_id", "desktop_pr_workflow", "desktop_branch_selections", "desktop_tag_rules",
}
_SECRET_FIELD_NAMES = frozenset({
    "token", "tokens", "secret", "secrets", "privatekey", "credential",
    "credentials", "password",
    "passwd", "devicecode", "usercode", "authorization", "pat",
    "personalaccesstoken", "accesstoken", "refreshtoken", "bearertoken",
    "idtoken", "apikey", "accesskey", "secretkey", "clientsecret",
    "authorizationcode", "accesstokens", "refreshtokens", "apikeys",
    "accesskeys", "clientsecrets",
})
_SECRET_FIELD_PREFIXES = ("github", "gh", "oauth")
_SECRET_FIELD_ALIASES = _SECRET_FIELD_NAMES | frozenset(
    prefix + name for prefix in _SECRET_FIELD_PREFIXES for name in _SECRET_FIELD_NAMES
)


def install_path(module_file=None):
    """Return the ignored preferences file at the application root."""
    location = Path(module_file).resolve() if module_file else Path(__file__).resolve()
    directory = location.parent
    root = directory.parent if directory.name == "app" else directory
    return root / ".preferences.json"


def validate_client_id(value):
    # GitHub has issued both alphanumeric client IDs and Iv1.-prefixed IDs.
    # Hyphenated identifiers are retained for synthetic/offline fixtures.
    # This is syntax validation, not proof of registration.
    return (isinstance(value, str) and len(value) <= 256
            and re.fullmatch(r"(?:[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*|Iv1\.[A-Za-z0-9]+)", value) is not None)


class PreferencesError(Exception):
    """A fixed, safe-to-display preferences error."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PreferencesError(
                "preferences_duplicate_key",
                "The preferences file contains duplicate JSON keys.",
            )
        result[key] = value
    return result


def _reject_non_json_constant(value):
    raise ValueError("Non-standard JSON constant")


def _check_unicode(value):
    if isinstance(value, str):
        if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise PreferencesError(
                "preferences_invalid",
                "Preferences contain invalid Unicode text.",
            )
    elif isinstance(value, dict):
        for key, item in value.items():
            _check_unicode(key)
            _check_unicode(item)
    elif isinstance(value, list):
        for item in value:
            _check_unicode(item)


def _reject_secret_fields(value):
    if isinstance(value, dict):
        for key, item in value.items():
            # Split separators and camel/Pascal case before matching whole
            # words or compound aliases. Avoid substrings such as "pat" in
            # "pattern" or "secret" in "secretary".
            words = re.sub(
                r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])",
                " ", key,
            )
            words = re.findall(r"[a-z0-9]+", words.lower())
            if any(
                "".join(words[start:end]) in _SECRET_FIELD_ALIASES
                for start in range(len(words))
                for end in range(start + 1, min(start + 3, len(words)) + 1)
            ):
                raise PreferencesError(
                    "preferences_secret_field",
                    "Preferences must not contain credentials or authorization codes.",
                )
            _reject_secret_fields(item)
    elif isinstance(value, list):
        for item in value:
            _reject_secret_fields(item)


def _validate_known_fields(value):
    string_fields = ("repo", "base", "base_ref")
    for field in string_fields:
        if field in value and not isinstance(value[field], str):
            raise PreferencesError(
                "preferences_invalid",
                "A text preference has an invalid value.",
            )
    if "reviewers" in value and not isinstance(value["reviewers"], (str, list)):
        raise PreferencesError(
            "preferences_invalid",
            "The reviewer preference has an invalid value.",
        )
    if "reviewers" in value and isinstance(value["reviewers"], list):
        if any(not isinstance(reviewer, str) for reviewer in value["reviewers"]):
            raise PreferencesError(
                "preferences_invalid",
                "The reviewer preference has an invalid value.",
            )
    if "desktop_configured" in value and type(value["desktop_configured"]) is not bool:
        raise PreferencesError(
            "preferences_invalid",
            "The desktop configuration preference has an invalid value.",
        )
    if ("github_app_client_id" in value
            and not validate_client_id(value["github_app_client_id"])):
        raise PreferencesError(
            "github_app_client_id_invalid",
            "The public GitHub App client ID is missing or invalid.",
        )

    rules = value.get("desktop_tag_rules", {})
    if not isinstance(rules, dict) or set(rules) - {"branch_filter", "suggestions"}:
        raise PreferencesError("preferences_invalid", "The local tag rules are invalid.")
    branch_filter = rules.get("branch_filter", "")
    suggestions = rules.get("suggestions", [])
    if (not isinstance(branch_filter, str) or len(branch_filter) > 255
            or any(ord(char) < 32 or ord(char) == 127 for char in branch_filter)
            or not isinstance(suggestions, list) or len(suggestions) > 50):
        raise PreferencesError("preferences_invalid", "The local tag rules are invalid.")
    rule_branches = set()
    for rule in suggestions:
        if (not isinstance(rule, dict) or set(rule) != {"branch", "prefix", "suffix"}
                or any(not isinstance(rule[key], str) for key in ("branch", "prefix", "suffix"))
                or not rule["branch"] or len(rule["branch"]) > 255
                or len(rule["prefix"]) > 100 or len(rule["suffix"]) > 100
                or any(ord(char) < 32 or ord(char) == 127
                       for key in ("branch", "prefix", "suffix") for char in rule[key])
                or rule["branch"] in rule_branches):
            raise PreferencesError("preferences_invalid", "A local tag suggestion rule is invalid.")
        rule_branches.add(rule["branch"])

    selections = value.get("desktop_branch_selections", [])
    if not isinstance(selections, list) or len(selections) > 1000:
        raise PreferencesError("preferences_invalid", "The saved branch selections are invalid.")
    seen_repositories = set()
    for selection in selections:
        if (not isinstance(selection, dict)
                or set(selection) != {"repository", "base_branch", "source_branch"}):
            raise PreferencesError("preferences_invalid", "A saved branch selection is invalid.")
        repository = selection["repository"]
        if (not isinstance(repository, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,39}/[A-Za-z0-9_.-]{1,100}", repository)
                or repository.split("/")[1] in (".", "..")
                or repository.casefold() in seen_repositories):
            raise PreferencesError("preferences_invalid", "A saved repository identity is invalid.")
        seen_repositories.add(repository.casefold())
        if any(not isinstance(selection[field], str) or len(selection[field]) > 255
               for field in ("base_branch", "source_branch")):
            raise PreferencesError("preferences_invalid", "A saved branch name is invalid.")

    if "desktop_pr_workflow" not in value:
        return
    workflow = value["desktop_pr_workflow"]
    if not isinstance(workflow, dict):
        raise PreferencesError(
            "preferences_invalid",
            "The desktop workflow preference is invalid.",
        )
    text_fields = ("repository", "base_branch", "source_branch", "title", "body",
                   "local_repository_path")
    for field in text_fields:
        if field in workflow and not isinstance(workflow[field], str):
            raise PreferencesError(
                "preferences_invalid",
                "A desktop workflow text preference has an invalid value.",
            )
    if len(workflow.get("local_repository_path", "")) > 4096:
        raise PreferencesError(
            "preferences_invalid",
            "The saved local repository path is too long.",
        )
    reviewers = workflow.get("reviewers")
    if reviewers is not None and (
        not isinstance(reviewers, list)
        or any(not isinstance(reviewer, str) for reviewer in reviewers)
    ):
        raise PreferencesError(
            "preferences_invalid",
            "The desktop workflow reviewer preference is invalid.",
        )


def _validate_document(value):
    if not isinstance(value, dict):
        raise PreferencesError(
            "preferences_invalid",
            "The preferences file must contain a JSON object.",
        )
    _check_unicode(value)
    _reject_secret_fields(value)
    _validate_known_fields(value)


def _validate_json_value(value):
    """Reject Python-only values before serializing an update."""
    if value is None or type(value) in (str, bool, int):
        _check_unicode(value)
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise PreferencesError(
                "preferences_invalid",
                "Preferences contain a non-finite number.",
            )
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise PreferencesError(
                    "preferences_invalid",
                    "Preference names must be text.",
                )
            _check_unicode(key)
            _validate_json_value(item)
        return
    raise PreferencesError(
        "preferences_invalid",
        "Preferences contain a value that cannot be saved as JSON.",
    )


def _read_raw(path, missing_ok=False):
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        if missing_ok:
            return None, {}
        raise PreferencesError(
            "github_app_unconfigured",
            "GitHub App sign-in is not configured. Add the public client ID to the installation preferences.",
        ) from None
    except OSError:
        raise PreferencesError(
            "preferences_unreadable",
            "The preferences file could not be read safely.",
        ) from None

    try:
        text = raw.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_json_constant,
        )
    except PreferencesError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise PreferencesError(
            "preferences_malformed",
            "The preferences file is malformed. Fix it before sign-in.",
        ) from None

    _validate_document(value)
    return raw, value


def read_preferences(path=None):
    path = Path(path) if path is not None else install_path()
    _, value = _read_raw(path)
    return value


def get_client_id(path=None):
    value = read_preferences(path)
    client_id = value.get("github_app_client_id")
    if not validate_client_id(client_id):
        raise PreferencesError(
            "github_app_unconfigured",
            "GitHub App sign-in is not configured. Add the public client ID to the installation preferences.",
        )
    return client_id.strip()


@contextmanager
def _interprocess_lock(path):
    """Lock a stable sidecar file so atomic replacement cannot swap the lock."""
    lock_path = path.with_name(path.name + ".lock")
    descriptor = None
    handle = None
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        handle = os.fdopen(descriptor, "r+b")
        descriptor = None
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except PreferencesError:
        raise
    except OSError:
        raise PreferencesError(
            "preferences_save_failed",
            "The preferences could not be locked safely. The existing file was not replaced.",
        ) from None
    finally:
        if handle is not None:
            handle.close()
        if descriptor is not None:
            os.close(descriptor)


def _merge_preferences(existing, changes, merge):
    updated = dict(existing) if merge else {}
    for key, value in changes.items():
        if key not in SUPPORTED_FIELDS:
            continue
        if key == "desktop_pr_workflow" and merge and isinstance(value, dict):
            previous = updated.get(key)
            combined = dict(previous) if isinstance(previous, dict) else {}
            combined.update(value)
            updated[key] = combined
        elif key == "desktop_branch_selections" and merge:
            _validate_known_fields({key: value})
            previous = updated.get(key, [])
            combined = list(previous)
            positions = {item["repository"].casefold(): index for index, item in enumerate(combined)}
            for item in value:
                repo_key = item["repository"].casefold()
                if repo_key in positions:
                    combined[positions[repo_key]] = dict(item)
                else:
                    positions[repo_key] = len(combined)
                    combined.append(dict(item))
            updated[key] = combined
        else:
            updated[key] = value
    _validate_document(updated)
    return updated


def save_preferences(changes, path=None, merge=True, *, only_if_missing=False):
    """Merge validated changes under a process and inter-process write lock."""
    path = Path(path) if path is not None else install_path()
    if only_if_missing and not merge:
        raise PreferencesError("preferences_invalid", "Only-if-missing updates require merge mode.")
    if not isinstance(changes, dict):
        raise PreferencesError(
            "preferences_invalid",
            "Preferences changes must be a JSON object.",
        )
    _validate_json_value(changes)
    _reject_secret_fields(changes)

    with _WRITE_LOCK:
        with _interprocess_lock(path):
            original_raw, existing = _read_raw(path, missing_ok=True)
            effective_changes = ({key: value for key, value in changes.items() if key not in existing}
                                 if only_if_missing else changes)
            if only_if_missing and not effective_changes:
                return existing
            updated = _merge_preferences(existing, effective_changes, merge)
            temp_path = None
            try:
                descriptor, temp_name = tempfile.mkstemp(
                    prefix=".preferences.", suffix=".tmp", dir=str(path.parent)
                )
                temp_path = Path(temp_name)
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                    json.dump(updated, handle, ensure_ascii=False, indent=2, allow_nan=False)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())

                current_raw = path.read_bytes() if path.exists() else None
                if current_raw != original_raw:
                    raise PreferencesError(
                        "preferences_conflict",
                        "The preferences changed while they were being saved. Reload them and try again.",
                    )
                os.replace(temp_path, path)
                temp_path = None
            except PreferencesError:
                raise
            except (OSError, UnicodeEncodeError, TypeError, ValueError):
                raise PreferencesError(
                    "preferences_save_failed",
                    "The preferences could not be saved safely. The existing file was not replaced.",
                ) from None
            finally:
                if temp_path is not None:
                    try:
                        temp_path.unlink()
                    except OSError:
                        pass
