"""Bounded GitHub Device Flow and remote PR operations.

This module accepts credentials only from the owning local server and never
persists them or returns them to a browser.
"""

from __future__ import annotations

import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


API_URL = "https://api.github.com"
DEVICE_CODE_URL = "https://github.com/login/device/code"
DEVICE_TOKEN_URL = "https://github.com/login/oauth/access_token"
VERIFY_URL = "https://github.com/login/device"
API_VERSION = "2022-11-28"
PAGE_SIZE = 100
MAX_PAGES = 20
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_PATCH_BYTES = 200_000
MAX_FILES = 300
MAX_COMMITS = 250
MAX_OPERATION_SECONDS = 60
POLL_MAX_SECONDS = 300
POLL_SLOW_DOWN = 5
MAX_TOKEN_LIFETIME_SECONDS = 315_360_000
COPILOT_REVIEWER_LOGIN = "copilot-pull-request-reviewer[bot]"
_TOKEN_SYNTAX = re.compile(r"[!-~]{1,8192}")
_TOKEN_TYPE_SYNTAX = re.compile(r"[A-Za-z0-9._-]{1,64}")
_SCOPE_SYNTAX = re.compile(r"[ -~]{0,1024}")

MAX_TITLE_SUBJECT = 120
MAX_SUMMARY_ENTRY = 300
MAX_SUMMARY_ENTRIES = 20
MAX_FILE_ENTRIES = 50
REDACTED = "[REDACTED]"

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
               re.DOTALL),
    re.compile(r"(?i)\b(?:password|passwd|pwd|secret|token|api[_-]?key|client[_-]?secret"
               r"|access[_-]?token|refresh[_-]?token|authorization)\b\s*[:=]\s*\S+"),
    re.compile(r"(?i)\b(?:set-cookie|cookie|session[_-]?id)\b\s*[:=]\s*\S+"),
    re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/@]+:[^\s/@]+@"),
)


def redact_text(value, limit=None):
    """Return plain, credential-free text; redaction runs before truncation."""
    if not isinstance(value, str):
        return ""
    text = _CONTROL_CHARACTERS.sub(" ", value)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if limit is not None and len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def contains_secret(value):
    """True when text carries credential-like content that must not be submitted."""
    if not isinstance(value, str):
        return False
    return any(pattern.search(value) for pattern in _SECRET_PATTERNS)


def _first_line(message):
    if not isinstance(message, str):
        return ""
    for line in message.splitlines():
        candidate = line.strip()
        if candidate:
            return candidate
    return ""


def build_default_title(commits, base, source):
    """Deterministic PR title from the remote comparison only."""
    subjects = [redact_text(_first_line(commit.get("message")), MAX_TITLE_SUBJECT)
                for commit in commits if isinstance(commit, dict)]
    subjects = [subject for subject in subjects if subject]
    title = subjects[0] if subjects else f"Merge {source} into {base}"
    if len(subjects) > 1:
        title = f"{title} (+{len(subjects)} commits)"
    return title[:256]


def build_default_body(commits, files, base, source, base_sha, source_sha, *, truncated=False):
    """Deterministic PR body from the remote comparison only."""
    subjects = [redact_text(_first_line(commit.get("message")), MAX_SUMMARY_ENTRY)
                for commit in commits if isinstance(commit, dict)]
    subjects = [subject for subject in subjects if subject]
    lines = ["## Summary", ""]
    if subjects:
        lines.extend(f"- {subject}" for subject in subjects[:MAX_SUMMARY_ENTRIES])
    else:
        lines.append("- No commit summary is available.")
    lines.extend(["", "## Changed files", ""])
    if files:
        for item in files[:MAX_FILE_ENTRIES]:
            status = item.get("status") if isinstance(item.get("status"), str) else ""
            additions = item.get("additions") if type(item.get("additions")) is int else 0
            deletions = item.get("deletions") if type(item.get("deletions")) is int else 0
            lines.append(f"- {redact_text(item.get('filename'), 4096)} ({status or 'changed'})"
                         f" +{additions}/-{deletions}")
    else:
        lines.append("- No changed files are available.")
    if truncated or len(subjects) > MAX_SUMMARY_ENTRIES or len(files) > MAX_FILE_ENTRIES:
        lines.extend(["", "_The commit or file list was truncated._"])
    lines.extend(["", f"Generated from the GitHub comparison of `{base}` and `{source}` at",
                  f"`{str(base_sha)[:7]}...{str(source_sha)[:7]}`."])
    return "\n".join(lines)



class APIError(Exception):
    """Fixed safe error suitable for an API response."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def _read_json(response, *, limit=MAX_RESPONSE_BYTES):
    raw = response.read(limit + 1)
    if len(raw) > limit:
        raise APIError("github_response_too_large", "GitHub returned too much data to process safely.")
    try:
        return json.loads(raw.decode("utf-8", "strict"))
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise APIError("github_invalid_response", "GitHub returned an invalid response.") from None


def _opener():
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    return build_opener(NoRedirect())


def _safe_transport_error(error, *, write=False):
    if isinstance(error, HTTPError):
        status = error.code
        headers = error.headers or {}
        sso = "required" in headers.get("X-GitHub-SSO", "").lower()
        rate_limited = status == 429 or headers.get("X-RateLimit-Remaining") == "0"
        error.close()
        if status == 401:
            return APIError("github_auth_failed", "GitHub authorization expired or was revoked; sign in again.", 401)
        if rate_limited:
            return APIError("github_rate_limit", "GitHub API rate limit reached. Retry later.", 429)
        if status in (403, 404):
            return APIError(
                "github_sso_required" if sso else "github_access_denied",
                "Complete organization approval or SSO for this GitHub App." if sso
                else "GitHub denied access. Check app approval and repository permissions.",
                status,
            )
        if write and status == 409:
            return APIError("github_pr_conflict", "GitHub reported a conflicting pull request. Inspect before retrying.", status)
        if write and status == 422:
            return APIError("github_pr_rejected", "GitHub rejected the pull request. Inspect before retrying.", status)
        if write and (status == 408 or status >= 500):
            return APIError("github_write_uncertain", "GitHub did not confirm the write. Inspect GitHub before retrying.", 502)
        return APIError("github_api_error", "GitHub returned an API error.", status)
    if write:
        return APIError("github_write_uncertain", "GitHub did not confirm the write. Inspect GitHub before retrying.", 502)
    return APIError("github_unavailable", "Could not reach GitHub. Retry the request.", 502)


def begin_device_flow(client_id: str):
    request = Request(
        DEVICE_CODE_URL,
        data=urlencode({"client_id": client_id}).encode("ascii"),
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": "github-pr-tool"},
    )
    try:
        with _opener().open(request, timeout=15) as response:
            payload = _read_json(response, limit=64 * 1024)
    except (HTTPError, URLError, OSError, TimeoutError, ValueError) as error:
        raise _safe_transport_error(error) from None
    if not isinstance(payload, dict):
        raise APIError("github_invalid_response", "GitHub returned invalid device authorization details.", 502)
    device_code, user_code = payload.get("device_code"), payload.get("user_code")
    uri, expires, interval = payload.get("verification_uri"), payload.get("expires_in"), payload.get("interval", 5)
    if (not isinstance(device_code, str) or not re.fullmatch(r"[!-~]{1,4096}", device_code)
            or not isinstance(user_code, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,32}", user_code)
            or uri != VERIFY_URL or type(expires) is not int or not 1 <= expires <= 86400
            or type(interval) is not int or not 1 <= interval <= POLL_MAX_SECONDS):
        raise APIError("github_invalid_response", "GitHub returned invalid device authorization details.", 502)
    return {"device_code": device_code, "user_code": user_code, "verification_uri": VERIFY_URL,
            "expires_in": expires, "interval": interval}


def _token_metadata(payload):
    """Validate actual optional token metadata without inventing lifetimes."""
    token_type = payload.get("token_type")
    scope = payload.get("scope")
    expires_in = payload.get("expires_in")
    refresh_token = payload.get("refresh_token")
    refresh_expires = payload.get("refresh_token_expires_in")
    if token_type is not None and (not isinstance(token_type, str)
                                   or not _TOKEN_TYPE_SYNTAX.fullmatch(token_type)):
        raise APIError("github_invalid_response", "GitHub returned invalid authorization metadata.", 502)
    if scope is not None and (not isinstance(scope, str) or not _SCOPE_SYNTAX.fullmatch(scope)):
        raise APIError("github_invalid_response", "GitHub returned invalid authorization metadata.", 502)
    if expires_in is not None and (type(expires_in) is not int
                                   or not 1 <= expires_in <= MAX_TOKEN_LIFETIME_SECONDS):
        raise APIError("github_invalid_response", "GitHub returned invalid authorization metadata.", 502)
    if refresh_token is not None and (not isinstance(refresh_token, str)
                                      or not _TOKEN_SYNTAX.fullmatch(refresh_token)):
        raise APIError("github_invalid_response", "GitHub returned invalid authorization metadata.", 502)
    if refresh_expires is not None and (type(refresh_expires) is not int
                                        or not 1 <= refresh_expires <= MAX_TOKEN_LIFETIME_SECONDS):
        raise APIError("github_invalid_response", "GitHub returned invalid authorization metadata.", 502)
    if refresh_expires is not None and refresh_token is None:
        raise APIError("github_invalid_response", "GitHub returned invalid authorization metadata.", 502)
    return {
        "token_type": token_type,
        "scope": scope,
        "expires_in": expires_in,
        "refresh_token": refresh_token,
        "refresh_token_expires_in": refresh_expires,
    }


def poll_device_flow(client_id: str, device_code: str, interval: int):
    request = Request(
        DEVICE_TOKEN_URL,
        data=urlencode({
            "client_id": client_id, "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        }).encode("ascii"),
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": "github-pr-tool"},
    )
    try:
        with _opener().open(request, timeout=15) as response:
            payload = _read_json(response, limit=64 * 1024)
    except (HTTPError, URLError, OSError, TimeoutError, ValueError) as error:
        raise _safe_transport_error(error) from None
    if not isinstance(payload, dict):
        raise APIError("github_invalid_response", "GitHub returned an invalid authorization response.", 502)
    error = payload.get("error")
    if error == "authorization_pending":
        return {"state": "pending", "interval": interval}
    if error == "slow_down":
        new_interval = min(POLL_MAX_SECONDS, max(interval + POLL_SLOW_DOWN, payload.get("interval", 0)
                                                 if type(payload.get("interval")) is int else 0))
        return {"state": "pending", "interval": new_interval}
    if error == "access_denied":
        raise APIError("github_auth_denied", "GitHub authorization was denied or cancelled.")
    if error == "expired_token":
        raise APIError("github_auth_expired", "The GitHub device code expired. Start sign-in again.")
    if error:
        raise APIError("github_auth_failed", "GitHub authorization did not complete. Start sign-in again.")
    token = payload.get("access_token")
    if not isinstance(token, str) or not _TOKEN_SYNTAX.fullmatch(token):
        raise APIError("github_invalid_response", "GitHub returned an invalid authorization response.", 502)
    metadata = _token_metadata(payload)
    return {"state": "authorized", "access_token": token, **metadata}


def refresh_token(client_id: str, refresh_token: str):
    """Exchange a stored refresh token using only the public client ID."""
    if not isinstance(refresh_token, str) or not _TOKEN_SYNTAX.fullmatch(refresh_token):
        raise APIError("refresh_rejected", "The saved GitHub authorization cannot be refreshed. Sign in again.", 401)
    request = Request(
        DEVICE_TOKEN_URL,
        data=urlencode({
            "client_id": client_id, "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }).encode("ascii"),
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": "github-pr-tool"},
    )
    try:
        with _opener().open(request, timeout=15) as response:
            payload = _read_json(response, limit=64 * 1024)
    except (HTTPError, URLError, OSError, TimeoutError, ValueError) as error:
        raise _safe_transport_error(error) from None
    if not isinstance(payload, dict):
        raise APIError("github_invalid_response", "GitHub returned an invalid authorization response.", 502)
    if payload.get("error"):
        raise APIError("refresh_rejected", "The saved GitHub authorization cannot be refreshed. Sign in again.", 401)
    token = payload.get("access_token")
    if not isinstance(token, str) or not _TOKEN_SYNTAX.fullmatch(token):
        raise APIError("github_invalid_response", "GitHub returned an invalid authorization response.", 502)
    metadata = _token_metadata(payload)
    return {"access_token": token, **metadata}


def verify_identity(token: str):
    payload, _ = _request(token, "GET", "/user")
    login = payload.get("login") if isinstance(payload, dict) else None
    # Enterprise Managed User logins are suffixed with _<enterprise-shortcode>.
    if not isinstance(login, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,39}", login):
        raise APIError("github_identity_failed", "GitHub did not return a valid account identity.", 502)
    return login


def _request(token, method, path, body=None, *, write=False, timeout=20):
    parsed = urlparse(API_URL + path)
    if parsed.scheme != "https" or parsed.hostname != "api.github.com" or parsed.port:
        raise APIError("github_api_error", "GitHub API host is not trusted.", 502)
    data = None if body is None else json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "github-pr-tool",
        "X-GitHub-Api-Version": API_VERSION,
    }
    request = Request(API_URL + path, data=data, headers=headers, method=method)
    try:
        with _opener().open(request, timeout=timeout) as response:
            try:
                payload = _read_json(response)
            except APIError:
                if write:
                    raise APIError(
                        "github_write_uncertain",
                        "GitHub did not confirm the write. Inspect GitHub before retrying.", 502,
                    ) from None
                raise
            return payload, response.headers
    except APIError:
        raise
    except (HTTPError, URLError, OSError, TimeoutError, ValueError) as error:
        raise _safe_transport_error(error, write=write) from None


def _repo_parts(repository):
    if not isinstance(repository, str):
        raise APIError("invalid_repository", "Select a repository returned by GitHub.")
    parts = repository.split("/")
    if (len(parts) != 2 or not re.fullmatch(r"[A-Za-z0-9_-]{1,39}", parts[0])
            or parts[1] in (".", "..") or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", parts[1])):
        raise APIError("invalid_repository", "Select a repository returned by GitHub.")
    return parts[0], parts[1]


def _branch_valid(branch):
    if (not isinstance(branch, str) or not 1 <= len(branch) <= 255
            or branch in (".", "..", "@") or ".." in branch
            or branch.startswith(("-", "/")) or branch.endswith(("/", "."))
            or "//" in branch or "@{" in branch
            or re.search(r"[\x00-\x20\x7f~^:?*\\\[]", branch)):
        return False
    return all(not part.startswith(".") and not part.endswith(".lock")
               for part in branch.split("/"))


def _patch_is_complete(patch, item):
    """Conservatively verify hunk and file counts before authorizing a write."""
    additions, deletions, changes = (item.get(key) for key in ("additions", "deletions", "changes"))
    if (any(type(value) is not int or value < 0 for value in (additions, deletions, changes))
            or changes != additions + deletions):
        return False
    old_left = new_left = None
    seen_additions = seen_deletions = 0
    saw_hunk = False
    lines = patch.split("\n")
    if lines[-1] == "":
        lines.pop()
    for line in lines:
        if line.startswith("@@ "):
            if old_left not in (None, 0) or new_left not in (None, 0):
               return False
            match = re.match(r"^@@ -[0-9]{1,10}(?:,([0-9]{1,10}))? \+[0-9]{1,10}(?:,([0-9]{1,10}))? @@(?:.*)$", line)
            if not match:
               return False
            old_left = int(match.group(1) or "1")
            new_left = int(match.group(2) or "1")
            saw_hunk = True
        elif line == r"\ No newline at end of file":
            if not saw_hunk:
               return False
        elif old_left is None or not line:
            return False
        elif line[0] == " ":
            old_left -= 1
            new_left -= 1
        elif line[0] == "-":
            old_left -= 1
            seen_deletions += 1
        elif line[0] == "+":
            new_left -= 1
            seen_additions += 1
        else:
            return False
        if old_left is not None and (old_left < 0 or new_left < 0):
            return False
    return (saw_hunk and old_left == 0 and new_left == 0
            and seen_additions == additions and seen_deletions == deletions)


def _next_page(headers, page, path, expected_params=None, *, repository_id=None):
    link = headers.get("Link", "") if headers else ""
    targets = re.findall(r'<([^<>]+)>\s*;\s*rel="([^"]+)"', str(link))
    next_links = [url for url, rel in targets if "next" in rel.split()]
    if len(next_links) > 1:
        raise APIError("pagination_invalid", "GitHub returned an invalid pagination link.", 502)
    if not next_links:
        if re.search(r"\brel\s*=\s*\"?next\b", str(link), re.I):
            raise APIError("pagination_invalid", "GitHub returned an invalid pagination link.", 502)
        return None
    try:
        parsed = urlparse(next_links[0])
        parsed_port = parsed.port
    except ValueError:
        raise APIError("pagination_invalid", "GitHub returned an unsafe pagination link.", 502) from None
    allowed_paths = {path}
    if type(repository_id) is int and repository_id > 0:
        canonical = re.match(r"^/repos/[^/]+/[^/]+(?P<suffix>/.*)?$", path)
        if canonical:
            allowed_paths.add(f"/repositories/{repository_id}{canonical.group('suffix') or ''}")
    if (parsed.scheme != "https" or parsed.hostname != "api.github.com" or parsed_port
            or parsed.username or parsed.password or parsed.fragment or parsed.path not in allowed_paths):
        raise APIError("pagination_invalid", "GitHub returned an unsafe pagination link.", 502)
    try:
        query = parse_qs(parsed.query, strict_parsing=True)
        npage = int(query["page"][0])
        expected_keys = {"page", "per_page"} | set(expected_params or {})
        if (set(query) != expected_keys or len(query["page"]) != 1
                or query.get("per_page") != [str(PAGE_SIZE)]
                or any(query.get(key) != values for key, values in (expected_params or {}).items())
                or npage != page + 1 or npage > MAX_PAGES):
            raise ValueError
    except (KeyError, ValueError, TypeError):
        raise APIError("pagination_invalid", "GitHub returned an invalid pagination sequence.", 502) from None
    return npage


class GitHubAPI:
    """Remote-only GitHub operations for one already-verified account."""

    def __init__(self, token):
        if not isinstance(token, str) or not re.fullmatch(r"[!-~]{1,8192}", token):
            raise APIError("github_auth_required", "Sign in to GitHub before using online PR actions.", 401)
        self.token = token
        self.deadline = time.monotonic() + MAX_OPERATION_SECONDS
        self._repository_ids = {}

    def _get(self, path, body=None, *, write=False, method=None):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise APIError("github_operation_timeout", "The GitHub operation took too long. Retry from the current selection.", 504)
        return _request(self.token, method or ("POST" if body is not None else "GET"),
                        path, body, write=write, timeout=min(20, remaining))

    @staticmethod
    def _page(page):
        if type(page) is not int or not 1 <= page <= MAX_PAGES:
            raise APIError("invalid_page", "The requested page is invalid.")

    def _repository_id(self, repository):
        owner, name = _repo_parts(repository)
        key = (owner.lower(), name.lower())
        if key not in self._repository_ids:
            payload, _ = self._get(
                f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}")
            repository_id = payload.get("id") if isinstance(payload, dict) else None
            if type(repository_id) is not int or repository_id <= 0:
                raise APIError("github_invalid_response", "GitHub returned invalid repository metadata.", 502)
            self._repository_ids[key] = repository_id
        return self._repository_ids[key]

    def _repository_id_for_pagination(self, headers, repository):
        link = headers.get("Link", "") if headers else ""
        if not re.search(r"<https://api\.github\.com/repositories/[0-9]+/", str(link)):
            return None
        return self._repository_id(repository)

    def repositories(self, page=1):
        self._page(page)
        path = "/user/repos"
        payload, headers = self._get(f"{path}?per_page={PAGE_SIZE}&page={page}")
        if not isinstance(payload, list):
            raise APIError("github_invalid_response", "GitHub did not return a repository list.", 502)
        items = []
        for item in payload:
            owner = item.get("owner", {}).get("login") if isinstance(item, dict) and isinstance(item.get("owner"), dict) else None
            name = item.get("name") if isinstance(item, dict) else None
            if (not isinstance(owner, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,39}", owner)
                    or not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", name)
                    or type(item.get("private")) is not bool):
                raise APIError("github_invalid_response", "GitHub returned an invalid repository entry.", 502)
            items.append({"full_name": owner + "/" + name, "private": item["private"]})
        next_page = _next_page(headers, page, path)
        return {"repositories": items, "page": page, "next_page": next_page, "complete": next_page is None}

    def branches(self, repository, page=1):
        owner, name = _repo_parts(repository)
        self._page(page)
        path = f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}/branches"
        payload, headers = self._get(f"{path}?per_page={PAGE_SIZE}&page={page}")
        if not isinstance(payload, list):
            raise APIError("github_invalid_response", "GitHub did not return a branch list.", 502)
        branches = []
        for item in payload:
            commit = item.get("commit") if isinstance(item, dict) else None
            sha = commit.get("sha") if isinstance(commit, dict) else None
            branch = item.get("name") if isinstance(item, dict) else None
            if not _branch_valid(branch) or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{7,64}", sha):
                raise APIError("github_invalid_response", "GitHub returned an invalid branch entry.", 502)
            branches.append({"name": branch, "sha": sha})
        repository_id = self._repository_id_for_pagination(headers, owner + "/" + name)
        next_page = _next_page(headers, page, path, repository_id=repository_id)
        return {"repository": owner + "/" + name, "branches": branches, "page": page,
                "next_page": next_page, "complete": next_page is None}

    def workflow_runs(self, repository, page=1, *, workflow_name="build-and-deploy"):
        owner, name = _repo_parts(repository)
        self._page(page)
        if not isinstance(workflow_name, str) or not workflow_name.strip():
            raise APIError("invalid_request", "Select a valid workflow name.")
        workflow_name = workflow_name.strip()
        workflow_ref = (quote(workflow_name, safe="") + ".yaml") if "." not in workflow_name else quote(workflow_name, safe="")
        path = f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}/actions/workflows/{workflow_ref}/runs"
        try:
            payload, headers = self._get(f"{path}?per_page={PAGE_SIZE}&page={page}")
        except APIError as error:
            if error.status == 404:
                raise APIError("workflow_unavailable", "Workflow not found or Actions access is unavailable.", 404) from None
            raise
        if not isinstance(payload, dict) or not isinstance(payload.get("workflow_runs"), list):
            raise APIError("github_invalid_response", "GitHub returned an invalid workflow run list.", 502)
        runs = []
        for item in payload["workflow_runs"]:
            if not isinstance(item, dict):
                raise APIError("github_invalid_response", "GitHub returned an invalid workflow run.", 502)
            run_id, number = item.get("id"), item.get("run_number")
            ref, sha = item.get("head_branch"), item.get("head_sha")
            if (type(run_id) is not int or run_id <= 0 or type(number) is not int or number <= 0
                    or not isinstance(ref, str) or not _branch_valid(ref)
                    or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", sha)):
                # GitHub can return runs without a head ref, e.g. deleted refs.
                # They cannot be verified against a current tag and are omitted.
                continue
            attempt = item.get("run_attempt")
            actor = item.get("actor")
            runs.append({"id": run_id, "run_number": number,
                         "run_attempt": attempt if type(attempt) is int and attempt > 0 else 1,
                         "ref": ref, "sha": sha,
                         "event": redact_text(item.get("event"), 40),
                         "status": redact_text(item.get("status"), 40),
                         "conclusion": redact_text(item.get("conclusion"), 40),
                         "created_at": redact_text(item.get("created_at"), 64),
                         "actor": redact_text(actor.get("login"), 39) if isinstance(actor, dict) else "",
                         "url": f"https://github.com/{owner}/{name}/actions/runs/{run_id}"})
        repository_id = self._repository_id_for_pagination(headers, repository)
        if page == MAX_PAGES and headers and re.search(r'\brel\s*=\s*"?next\b', str(headers.get("Link", "")), re.I):
            next_page, limited = None, True
        else:
            next_page = _next_page(headers, page, path, repository_id=repository_id)
            limited = False
        return {"runs": runs, "next_page": next_page, "scan_limited": limited}

    def workflow_run_jobs(self, repository, run_id):
        owner, name = _repo_parts(repository)
        if type(run_id) is not int or run_id <= 0:
            raise APIError("invalid_request", "Select a valid workflow run.")
        path = f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}/actions/runs/{run_id}/jobs"
        jobs = []
        page = 1
        while True:
            payload, headers = self._get(f"{path}?per_page={PAGE_SIZE}&page={page}")
            if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
                raise APIError("github_invalid_response", "GitHub returned an invalid workflow job list.", 502)
            for item in payload["jobs"]:
                if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                    raise APIError("github_invalid_response", "GitHub returned an invalid workflow job.", 502)
                name_text = redact_text(item["name"], 300)
                if not re.search(r"\bdeploy\b", name_text, re.I):
                    continue
                match = re.fullmatch(r".*\(([^()]+)\)\s*/\s*deploy\s*", name_text, re.I)
                status = redact_text(item.get("status"), 40)
                conclusion = redact_text(item.get("conclusion"), 40)
                if status == "completed":
                    state = conclusion or "unknown"
                elif status in ("waiting", "queued", "requested", "pending"):
                    state = "waiting"
                elif status == "in_progress":
                    state = "in_progress"
                else:
                    state = "unknown"
                steps = item.get("steps")
                sync_confirmed = isinstance(steps, list) and any(
                    isinstance(step, dict)
                    and isinstance(step.get("name"), str)
                    and step["name"].casefold() == "sync the argo cd application and wait for a healthy state"
                    and step.get("status") == "completed" and step.get("conclusion") == "success"
                    for step in steps)
                jobs.append({"environment": match.group(1).strip() if match else "",
                             "job_name": name_text, "state": state, "sync_confirmed": sync_confirmed})
            repository_id = self._repository_id_for_pagination(headers, repository)
            next_page = _next_page(headers, page, path, repository_id=repository_id)
            if next_page is None:
                return jobs
            page = next_page

    def branch_tip(self, repository, branch):
        owner, name = _repo_parts(repository)
        if not _branch_valid(branch):
            raise APIError("invalid_branch", "Select a valid remote branch.")
        path = (f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}/branches/"
                f"{quote(branch, safe='/')}")
        try:
            payload, _ = self._get(path)
        except APIError as error:
            if error.status == 404:
                raise APIError("branch_not_found", "The selected remote branch no longer exists.", 404) from None
            raise
        commit = payload.get("commit") if isinstance(payload, dict) else None
        sha = commit.get("sha") if isinstance(commit, dict) else None
        if (not isinstance(payload, dict) or payload.get("name") != branch
                or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", sha)):
            raise APIError("github_invalid_response", "GitHub returned invalid branch metadata.", 502)
        return {"name": branch, "sha": sha}

    def create_tag(self, repository, tag, sha):
        owner, name = _repo_parts(repository)
        if (not _branch_valid(tag) or not isinstance(sha, str)
                or not re.fullmatch(r"[0-9a-fA-F]{40}", sha)):
            raise APIError("invalid_tag", "Enter a valid tag name and target commit.")
        ref = "refs/tags/" + tag
        ref_path = (f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}/git/ref/tags/"
                    f"{quote(tag, safe='/')}")
        try:
            existing, _ = self._get(ref_path)
        except APIError as error:
            if error.status != 404:
                raise
        else:
            if isinstance(existing, dict) and existing.get("ref") == ref:
                raise APIError("tag_exists", "A tag with that name already exists on GitHub.", 409)
            raise APIError("github_invalid_response", "GitHub returned an unexpected tag reference.", 502)

        path = f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}/git/refs"
        try:
            payload, _ = self._get(
                path, {"ref": ref, "sha": sha},
                write=True, method="POST")
        except APIError as error:
            if error.status == 422:
                raise APIError("tag_exists", "A tag with that name already exists on GitHub.", 409) from None
            raise
        ref = payload.get("ref") if isinstance(payload, dict) else None
        obj = payload.get("object") if isinstance(payload, dict) else None
        returned_sha = obj.get("sha") if isinstance(obj, dict) else None
        if ref != "refs/tags/" + tag or returned_sha != sha:
            raise APIError("github_write_uncertain", "GitHub did not confirm the tag. Inspect before retrying.", 502)
        return {"created": True, "tag": tag, "sha": sha}

    def _branch_tips(self, repository, selected):
        tips = {}
        page = 1
        while True:
            result = self.branches(repository, page)
            tips.update({x["name"]: x["sha"] for x in result["branches"] if x["name"] in selected})
            if selected.issubset(tips):
                return tips
            if result["complete"]:
                raise APIError("branch_not_found", "A selected remote branch is no longer available. Reload branches.")
            page = result["next_page"]

    def compare(self, repository, base, source):
        owner, name = _repo_parts(repository)
        if not _branch_valid(base) or not _branch_valid(source) or base == source:
            raise APIError("invalid_branch", "Select distinct, valid remote branches.")
        selected = {base, source}
        before = self._branch_tips(repository, selected)
        if not all(re.fullmatch(r"[0-9a-fA-F]{40}", before[x]) for x in selected):
            raise APIError("comparison_incomplete", "GitHub did not return full remote commit IDs.")
        path = f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}/compare/{quote(base, safe='/')}...{quote(source, safe='/')}"
        payload, headers = self._get(f"{path}?per_page={PAGE_SIZE}&page=1")
        if not isinstance(payload, dict) or not isinstance(payload.get("files"), list) or not isinstance(payload.get("commits"), list):
            raise APIError("github_invalid_response", "GitHub did not return a remote comparison.", 502)
        base_commit = payload.get("base_commit")
        head_commit = payload.get("head_commit")
        files = payload["files"]
        commits = list(payload["commits"])
        total = payload.get("total_commits")
        # GitHub may omit head_commit for a comparison, so verify each side only
        # when the response actually carries it. The branch-tip re-check below
        # still detects a branch that moves during the comparison.
        base_sha = base_commit.get("sha") if isinstance(base_commit, dict) else None
        if base_sha is not None and base_sha != before[base]:
            raise APIError("branch_changed", "A remote branch changed during comparison. Reload and compare again.")
        head_sha = head_commit.get("sha") if isinstance(head_commit, dict) else None
        if head_sha is None and type(total) is int and total == len(commits) and commits:
            last = commits[-1]
            head_sha = last.get("sha") if isinstance(last, dict) else None
        if head_sha is not None and head_sha != before[source]:
            raise APIError("branch_changed", "A remote branch changed during comparison. Reload and compare again.")
        page = 1
        while type(total) is int and len(commits) < min(total, MAX_COMMITS):
            repository_id = self._repository_id_for_pagination(headers, repository)
            following = _next_page(headers, page, path, repository_id=repository_id)
            if following is None:
                break
            data, headers = self._get(f"{path}?per_page={PAGE_SIZE}&page={following}")
            if (not isinstance(data, dict) or not isinstance(data.get("commits"), list)
                    or data.get("total_commits") != total):
                raise APIError("comparison_incomplete", "GitHub returned inconsistent comparison pages.", 502)
            commits.extend(data["commits"])
            page = following
            if page >= 3:
                break
        repository_id = self._repository_id_for_pagination(headers, repository)
        commits_complete = (type(total) is int and total == len(commits) and total < MAX_COMMITS
                            and _next_page(headers, page, path, repository_id=repository_id) is None)
        after = self._branch_tips(repository, selected)
        if before != after:
            raise APIError("branch_changed", "A remote branch changed during comparison. Reload and compare again.")
        files_complete = len(files) < MAX_FILES
        result_files, result_commits = [], []
        for item in files[:MAX_FILES]:
            if not isinstance(item, dict) or not isinstance(item.get("filename"), str) or len(item["filename"]) > 4096:
                raise APIError("github_invalid_response", "GitHub returned an invalid comparison entry.", 502)
            patch = item.get("patch")
            patch_status = "unavailable" if patch is None else "available"
            if isinstance(patch, str):
                try:
                    patch_size = len(patch.encode("utf-8"))
                except UnicodeEncodeError:
                    raise APIError("github_invalid_response", "GitHub returned an invalid comparison entry.", 502) from None
                if patch_size > MAX_PATCH_BYTES:
                    patch, patch_status = None, "oversized"
            if patch is not None and not isinstance(patch, str):
                raise APIError("github_invalid_response", "GitHub returned an invalid comparison entry.", 502)
            if patch is not None and not _patch_is_complete(patch, item):
                patch, patch_status = None, "unavailable"
            result_files.append({
                "filename": redact_text(item["filename"], 4096),
                "status": item.get("status") if isinstance(item.get("status"), str) else "",
                "additions": item.get("additions") if type(item.get("additions")) is int else None,
                "deletions": item.get("deletions") if type(item.get("deletions")) is int else None,
                "changes": item.get("changes") if type(item.get("changes")) is int else None,
                "patch": patch, "patch_status": patch_status,
            })
        for item in commits[:MAX_COMMITS]:
            commit = item.get("commit") if isinstance(item, dict) else None
            sha = item.get("sha") if isinstance(item, dict) else None
            if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{7,64}", sha):
                raise APIError("github_invalid_response", "GitHub returned an invalid comparison entry.", 502)
            message = commit.get("message", "") if isinstance(commit, dict) else ""
            result_commits.append({"sha": sha, "message": redact_text(message, 2000)})
        status = payload.get("status")
        if status not in ("identical", "ahead", "behind", "diverged"):
            status = None
        ahead_by = payload.get("ahead_by")
        behind_by = payload.get("behind_by")
        if type(ahead_by) is not int or not 0 <= ahead_by <= 10_000_000:
            ahead_by = None
        if type(behind_by) is not int or not 0 <= behind_by <= 10_000_000:
            behind_by = None
        complete = (status is not None and ahead_by is not None and behind_by is not None
                    and files_complete and commits_complete
                    and all(item["patch_status"] == "available" for item in result_files))
        return {
            "repository": repository, "base": base, "source": source,
            "base_sha": before[base], "source_sha": before[source],
            "status": status, "ahead_by": ahead_by,
            "behind_by": behind_by, "commits": result_commits,
            "files": result_files, "complete": complete, "truncated": not complete,
            "files_complete": files_complete, "commits_complete": commits_complete,
        }

    def reviewers(self, repository, query, cursor=None):
        owner, name = _repo_parts(repository)
        if not isinstance(query, str) or not query.strip() or len(query) > 100:
            raise APIError("invalid_query", "Enter a reviewer name or GitHub login to search.")
        if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 1024):
            raise APIError("invalid_cursor", "The reviewer page is invalid.")
        gql = """query($owner:String!,$repo:String!,$search:String!,$after:String){
          repository(owner:$owner,name:$repo){
            assignableUsers(first:50,query:$search,after:$after){
              nodes{login name} pageInfo{hasNextPage endCursor}
            }
          }
        }"""
        payload, _ = self._get("/graphql", {"query": gql, "variables": {
            "owner": owner, "repo": name, "search": query.strip(), "after": cursor,
        }})
        try:
            candidates = payload["data"]["repository"]["assignableUsers"]
            nodes, info = candidates["nodes"], candidates["pageInfo"]
            if not isinstance(nodes, list) or not isinstance(info.get("hasNextPage"), bool):
                raise ValueError
            users = []
            for entry in nodes:
                login = entry.get("login") if isinstance(entry, dict) else None
                if isinstance(login, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,39}", login):
                    users.append({"login": login, "name": entry.get("name") if isinstance(entry.get("name"), str) else ""})
            end_cursor = info.get("endCursor")
            if info["hasNextPage"] and (not isinstance(end_cursor, str) or not end_cursor or len(end_cursor) > 1024):
                raise ValueError
            return {"reviewers": users, "next_cursor": end_cursor if info["hasNextPage"] else None,
                    "complete": not info["hasNextPage"]}
        except (KeyError, TypeError, ValueError):
            raise APIError("github_invalid_response", "GitHub did not return assignable reviewers.", 502) from None

    def request_copilot_review(self, repository, pull_number):
        """Request a Copilot code review using GitHub's dedicated REST endpoint."""
        owner, name = _repo_parts(repository)
        if type(pull_number) is not int or pull_number <= 0:
            raise APIError("invalid_attempt", "The pull request number is invalid.")
        path = (f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
                f"/pulls/{pull_number}/copilot-code-review")
        payload, _ = self._get(path, write=True, method="POST")
        if not isinstance(payload, dict) or payload.get("state") != "PENDING":
            raise APIError(
                "github_write_uncertain",
                "GitHub did not confirm the Copilot review request. Inspect the pull request before retrying.",
                502,
            )
        return {"requested": True, "confirmed": True, "state": "PENDING"}

    def inspect(self, repository, base, source):
        owner, name = _repo_parts(repository)
        if not _branch_valid(base) or not _branch_valid(source) or base == source:
            raise APIError("invalid_branch", "Select distinct, valid remote branches.")
        path = f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}/pulls"
        page = 1
        while True:
            payload, headers = self._get(
                f"{path}?state=open&base={quote(base, safe='')}&per_page={PAGE_SIZE}&page={page}")
            if not isinstance(payload, list):
                raise APIError("github_invalid_response", "GitHub did not return a pull request list.", 502)
            for item in payload:
                head = item.get("head") if isinstance(item, dict) else None
                base_info = item.get("base") if isinstance(item, dict) else None
                head_repository = head.get("repo") if isinstance(head, dict) else None
                base_repository = base_info.get("repo") if isinstance(base_info, dict) else None
                if (isinstance(head, dict) and isinstance(base_info, dict)
                        and isinstance(head_repository, dict) and isinstance(base_repository, dict)
                        and head_repository.get("full_name") == owner + "/" + name
                        and base_repository.get("full_name") == owner + "/" + name
                        and head.get("ref") == source and base_info.get("ref") == base):
                    return {"exists": True, "pull_request": self._pr_summary(item, owner + "/" + name)}
            repository_id = self._repository_id_for_pagination(headers, repository)
            following = _next_page(
                headers, page, path, {"state": ["open"], "base": [base]}, repository_id=repository_id)
            if following is None:
                return {"exists": False, "pull_request": None}
            page = following

    @staticmethod
    def _pr_summary(item, repository=None):
        number = item.get("number")
        url = item.get("html_url")
        title = item.get("title")
        expected_path = (f"/{repository}/pull/{number}" if repository is not None else None)
        try:
            parsed_url = urlparse(url) if isinstance(url, str) else None
            port = parsed_url.port if parsed_url is not None else None
        except ValueError:
            parsed_url, port = None, None
        if (type(number) is not int or parsed_url is None
                or parsed_url.scheme != "https" or parsed_url.hostname != "github.com"
                or parsed_url.username or parsed_url.password or port
                or parsed_url.fragment
                or expected_path is not None and parsed_url.path != expected_path):
            raise APIError("github_invalid_response", "GitHub returned an invalid pull request.", 502)
        return {"number": number, "url": url, "title": title if isinstance(title, str) else ""}

    def create(self, repository, base, source, title, body, reviewer_logins,
               expected_base_sha, expected_source_sha, *, on_write_start=None, on_write_end=None):
        if not isinstance(title, str) or not title.strip() or len(title) > 256 or not isinstance(body, str) or len(body) > 60000:
            raise APIError("invalid_content", "Enter a title up to 256 characters and a description up to 60000 characters.")
        if not isinstance(reviewer_logins, list) or len(reviewer_logins) > 50:
            raise APIError("invalid_reviewers", "The reviewer selection is invalid.")
        for login in reviewer_logins:
            if not isinstance(login, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,39}", login):
                raise APIError("invalid_reviewers", "The reviewer selection is invalid.")
        existing = self.inspect(repository, base, source)
        if existing["exists"]:
            return {"created": False, "existing": existing, "reviewers": {"requested": False}}
        fresh = self.compare(repository, base, source)
        if not fresh["complete"]:
            raise APIError("comparison_incomplete", "The remote comparison is incomplete; pull request creation is disabled.")
        if fresh["base_sha"] != expected_base_sha or fresh["source_sha"] != expected_source_sha:
            raise APIError("branch_changed", "Remote branches changed. Compare and inspect again.")
        if type(fresh.get("ahead_by")) is not int or fresh["ahead_by"] <= 0:
            raise APIError("no_changes", "The source branch has no new commits to include in a pull request.")
        owner, name = _repo_parts(repository)
        path = f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}/pulls"
        try:
            if on_write_start:
                on_write_start()
            payload, _ = self._get(
                path, {"title": title.strip(), "body": body, "head": source, "base": base}, write=True)
        finally:
            if on_write_end:
                on_write_end()
        try:
            pr = self._pr_summary(payload, owner + "/" + name)
        except (APIError, AttributeError, TypeError):
            raise APIError("github_write_uncertain", "GitHub did not confirm the pull request. Inspect before retrying.", 502) from None
        reviewer_result = {"requested": not reviewer_logins, "confirmed": not reviewer_logins, "failed_count": 0}
        if reviewer_logins:
            try:
                if on_write_start:
                    on_write_start()
                number = pr["number"]
                result, _ = self._get(
                    f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}/pulls/{number}/requested_reviewers",
                    {"reviewers": reviewer_logins}, write=True,
                )
                reviewer_result = {"requested": True, "confirmed": isinstance(result, dict), "failed_count": 0}
            except APIError as error:
                reviewer_result = {
                    "requested": True, "confirmed": False, "failed_count": len(reviewer_logins),
                    "error_code": error.code, "session_invalidated": error.code == "github_auth_failed",
                    "uncertain": error.code == "github_write_uncertain",
                }
            finally:
                if on_write_end:
                    on_write_end()
        return {"created": True, "pull_request": pr, "reviewers": reviewer_result}
