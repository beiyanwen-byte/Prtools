"""Backend adapter for the native local-repository PR GUI.

Git supplies the credential through its configured HTTPS credential helper. The
credential is held in memory only and is never returned to the GUI or logged.
"""

from __future__ import annotations

import base64
import os
import re
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

import github_api
import preferences_store


class DesktopBackendError(Exception):
    """Fixed, safe-to-display backend error."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


_OWNER_RE = re.compile(r"[A-Za-z0-9_-]{1,39}")
_REPO_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}")
_SCP_REMOTE_RE = re.compile(r"^(?:git@)?github\.com:([^/]+)/(.+)$", re.I)
COPILOT_REVIEWER_ALIAS = "copilot"
_GIT_ENV_TO_REMOVE = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
    "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
    "GIT_ASKPASS", "SSH_ASKPASS", "GIT_TRACE", "GIT_TRACE_SETUP",
    "GIT_TRACE_PACKET", "GIT_TRACE_CURL", "GIT_CURL_VERBOSE",
)


def _repository_from_remote(remote):
    """Return canonical repository and HTTPS credential URL, without echoing URL."""
    owner = name = None
    try:
        if remote.startswith(("https://", "http://", "ssh://")):
            parsed = urlsplit(remote)
            host = (parsed.hostname or "").lower()
            if host != "github.com" or parsed.query or parsed.fragment:
                raise ValueError
            if parsed.scheme == "https":
                if parsed.port not in (None, 443) or parsed.username or parsed.password:
                    raise ValueError
            elif parsed.scheme == "http":
                # Never send an HTTP-origin credential to an unencrypted host.
                raise ValueError
            elif parsed.scheme == "ssh":
                if parsed.port not in (None, 22) or parsed.password or parsed.username not in (None, "git"):
                    raise ValueError
            components = unquote(parsed.path).strip("/").split("/")
            if len(components) != 2:
                raise ValueError
            owner, name = components
        else:
            match = _SCP_REMOTE_RE.fullmatch(remote)
            if not match:
                raise ValueError
            owner, name = match.groups()
            name = unquote(name)
    except (ValueError, UnicodeError):
        raise DesktopBackendError(
            "unsupported_remote",
            "Select a repository whose origin is a GitHub.com HTTPS or SSH remote.",
        ) from None

    if name and name.endswith(".git"):
        name = name[:-4]
    if (not isinstance(owner, str) or not _OWNER_RE.fullmatch(owner)
            or not isinstance(name, str) or not _REPO_RE.fullmatch(name)
            or name in (".", "..")):
        raise DesktopBackendError(
            "unsupported_remote", "The selected GitHub remote is not a valid repository URL.")
    repository = f"{owner}/{name}"
    return repository, f"https://github.com/{owner}/{name}.git"


def _git_run(repo, *args, timeout=10, input_text=None, extra_env=None):
    env = os.environ.copy()
    for key in _GIT_ENV_TO_REMOVE:
        env.pop(key, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    if extra_env:
        env.update(extra_env)
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            input=input_text,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, check=False, env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise DesktopBackendError("git_unavailable", "Git could not read the selected repository.") from None
    return result


def _git_output(repo, *args, timeout=10):
    result = _git_run(repo, *args, timeout=timeout)
    if result.returncode != 0:
        # Git's stderr may contain a private path or remote URL; never forward it.
        raise DesktopBackendError("git_repository_invalid", "The selected folder is not a usable Git repository.")
    return result.stdout.strip()


def _credential_for_https_url(url, *, timeout=15):
    """Use Git's configured helper without reading its backing store directly."""
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != "github.com" or parsed.port
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise DesktopBackendError("unsupported_remote", "GitHub credentials are only requested for a fixed HTTPS host.")
    request = f"protocol=https\nhost=github.com\npath={parsed.path.lstrip('/')}\n\n"
    env = os.environ.copy()
    for key in _GIT_ENV_TO_REMOVE:
        env.pop(key, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    try:
        result = subprocess.run(
            ["git", "credential", "fill"], input=request,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, cwd=tempfile.gettempdir(), env=env, check=False,
        )
    except subprocess.TimeoutExpired:
        raise DesktopBackendError("credential_helper_timeout", "Git credential lookup timed out; configure a noninteractive Git credential helper.") from None
    except OSError:
        raise DesktopBackendError("credential_helper_unavailable", "Git credential lookup is unavailable.") from None
    if result.returncode != 0:
        raise DesktopBackendError("credential_missing", "No noninteractive GitHub HTTPS credential was returned by Git.")

    fields = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in ("username", "password"):
            fields[key] = value
    token = fields.get("password")
    if not isinstance(token, str) or not re.fullmatch(r"[!-~]{1,8192}", token):
        raise DesktopBackendError("credential_missing", "Git did not return a usable HTTPS token for this repository.")
    # Do not retain or return the subprocess output. Only return the secret to
    # the backend object, which will use it in Authorization headers in memory.
    return token


class DesktopBackend:
    """Stateful backend session for one selected local checkout and account."""

    def __init__(self):
        self.repository_path = None
        self.repository = None
        self.credential_url = None
        self.current_branch = ""
        self.token = None
        self.manual_token = False
        self.login = None
        self.repository_info = None
        self.branches = []
        self.fetched = False
        self.tags_fresh = False
        self.comparison = None
        self.inspection = None
        self.reviewer_candidates = set()
        self.copilot_reviewer_available = False
        self.workflow_state = None
        self.activity_records = []

    def _record(self, stage, outcome, detail):
        self.activity_records.append({
            "time": datetime.now().astimezone().strftime("%H:%M:%S"),
            "stage": stage, "outcome": outcome, "detail": detail,
        })
        del self.activity_records[:-100]
        # Bound the IPC payload even when a Fetch changes many branch names.
        while len(self.activity_records) > 1 and sum(
                len(item["detail"]) for item in self.activity_records) > 500_000:
            self.activity_records.pop(0)

    def activity(self):
        return {"records": [dict(record) for record in self.activity_records]}

    def sign_out(self):
        self.repository_path = None
        self.repository = None
        self.credential_url = None
        self.current_branch = ""
        self.token = None
        self.manual_token = False
        self.login = None
        self.repository_info = None
        self.branches = []
        self.fetched = False
        self.tags_fresh = False
        self.comparison = None
        self.inspection = None
        self.reviewer_candidates.clear()
        self.copilot_reviewer_available = False
        self.workflow_state = None
        # Activity is sanitized, session-only data and stays visible when the
        # user reconnects or selects another repository in the same worker.

    def describe_repository(self, path):
        try:
            selected = Path(path).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            raise DesktopBackendError("git_repository_invalid", "The selected folder is not available.") from None
        if not selected.is_dir():
            raise DesktopBackendError("git_repository_invalid", "Select a local Git repository folder.")
        root = Path(_git_output(selected, "rev-parse", "--show-toplevel")).resolve()
        remote = _git_output(root, "remote", "get-url", "origin")
        repository, _credential_url = _repository_from_remote(remote)
        return {"repository": repository, "root": str(root)}

    def connect_repository(self, path, *, token=None, expected_repository=None):
        self.sign_out()
        description = self.describe_repository(path)
        root = Path(description["root"])
        repository = description["repository"]
        if expected_repository is not None and repository.casefold() != expected_repository.casefold():
            raise DesktopBackendError("repository_changed", "Repository origin changed; reconnect before using the saved token.")
        credential_url = f"https://github.com/{repository}.git"
        try:
            current_branch = _git_output(root, "branch", "--show-current")
        except DesktopBackendError:
            current_branch = ""
        supplied = token is not None
        if supplied and (not isinstance(token, str) or not re.fullmatch(r"[!-~]{1,8192}", token)):
            raise DesktopBackendError("credential_invalid", "Enter a valid GitHub token.")
        if not supplied:
            token = _credential_for_https_url(credential_url)
        try:
            login = github_api.verify_identity(token)
            metadata, _ = github_api._request(
                token, "GET", f"/repos/{repository.split('/')[0]}/{repository.split('/')[1]}")
        except github_api.APIError as error:
            if error.code == "github_auth_failed":
                message = "GitHub rejected the Git credential. Refresh it with your Git credential helper, then reconnect."
            elif error.code in ("github_access_denied", "github_sso_required"):
                message = "This Git credential cannot access the selected repository through the GitHub API. Check repository permissions or organization approval."
            else:
                message = str(error)
            self.sign_out()
            raise DesktopBackendError(error.code, message) from None

        self.repository_path = str(root)
        self.repository = repository
        self.credential_url = credential_url
        self.current_branch = current_branch
        self.token = token
        self.manual_token = supplied
        self.login = login
        self.repository_info = {
            "repository": repository,
            "private": metadata.get("private") is True,
            "default_branch": metadata.get("default_branch") if isinstance(metadata.get("default_branch"), str) else "",
            "permissions": metadata.get("permissions") if isinstance(metadata.get("permissions"), dict) else {},
        }
        self._record("connect", "ok", "GitHub identity and selected repository verified.")
        return {**self.repository_info, "login": login, "current_branch": current_branch}

    def _api(self):
        if not self.token or not self.login or not self.repository:
            raise DesktopBackendError("github_auth_required", "Connect a local GitHub repository before online actions.")
        return github_api.GitHubAPI(self.token)

    def _raise_api_error(self, error):
        if error.code == "github_auth_failed":
            self.sign_out()
        raise DesktopBackendError(error.code, str(error)) from None

    def load_branches(self):
        if not self.repository_path or not self.repository:
            raise DesktopBackendError("git_repository_invalid", "Connect a local GitHub repository first.")
        refs = self._remote_ref_map()
        self.branches = [{"name": name, "sha": sha}
                         for name, sha in sorted(refs.items(), key=lambda item: item[0].casefold())]
        self._record("branches", "ok", f"Loaded {len(self.branches)} cached origin branches.")
        return self.branches

    def _remote_ref_map(self):
        output = _git_output(
            self.repository_path, "for-each-ref", "--format=%(refname:short)%09%(objectname)",
            "refs/remotes/origin")
        refs = {}
        for line in output.splitlines():
            name, separator, sha = line.partition("\t")
            if not separator or not name.startswith("origin/") or name == "origin/HEAD":
                continue
            branch = name[len("origin/"):]
            if github_api._branch_valid(branch) and re.fullmatch(r"[0-9a-fA-F]{40}", sha):
                refs[branch] = sha
        return refs

    def _tag_ref_map(self, namespace):
        output = _git_output(
            self.repository_path, "for-each-ref",
            "--format=%(refname)%09%(creatordate:unix)%09%(objectname)%09%(*objectname)",
            namespace)
        tags = {}
        for line in output.splitlines():
            fields = line.split("\t")
            # _git_output strips trailing whitespace, including the final tab
            # on a lightweight tag at the end of the output.
            if len(fields) == 3:
                fields.append("")
            if len(fields) != 4:
                continue
            ref, epoch_text, object_sha, peeled_sha = fields
            prefix = namespace + "/"
            if not ref.startswith(prefix):
                continue
            name = ref[len(prefix):]
            target_sha = peeled_sha or object_sha
            try:
                epoch = int(epoch_text)
            except ValueError:
                continue
            if name and re.fullmatch(r"[0-9a-fA-F]{40}", target_sha):
                created_at = datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")
                tags[name] = {"name": name, "created_at": created_at,
                              "created_epoch": epoch, "sha": target_sha}
        return tags

    def _fetch_output(self, *args, timeout=180):
        if not self.manual_token:
            return _git_output(self.repository_path, *args, timeout=timeout)
        # Git receives the validated token through its process environment, never
        # an argument, URL, config file, helper, or activity log. Redirects are
        # disabled so an authorization header cannot be forwarded to another host.
        basic = base64.b64encode(("x-access-token:" + self.token).encode("ascii")).decode("ascii")
        settings = [
            ("credential.helper", ""),
            ("http.followRedirects", "false"),
            ("http.https://github.com/.extraHeader", "Authorization: Basic " + basic),
            ("url.https://github.com/.insteadOf", "git@github.com:"),
            ("url.https://github.com/.insteadOf", "github.com:"),
            ("url.https://github.com/.insteadOf", "ssh://git@github.com/"),
            ("url.https://github.com/.insteadOf", "ssh://github.com/"),
        ]
        env = {"GIT_CONFIG_COUNT": str(len(settings))}
        for index, (key, value) in enumerate(settings):
            env[f"GIT_CONFIG_KEY_{index}"] = key
            env[f"GIT_CONFIG_VALUE_{index}"] = value
        result = _git_run(self.repository_path, *args, timeout=timeout, extra_env=env)
        if result.returncode:
            raise DesktopBackendError("git_fetch_failed", "Git could not fetch from origin with this credential.")
        return result.stdout.strip()

    def fetch_remote(self):
        if not self.repository_path or not self.repository:
            raise DesktopBackendError("git_repository_invalid", "Connect a local GitHub repository first.")
        self._record("fetch", "started", f"Repository {self.repository}: fetching origin remote branches.")
        self.fetched = False
        self.tags_fresh = False
        self.workflow_state = None
        try:
            before = self._remote_ref_map()
            self._fetch_output("fetch", "--no-recurse-submodules", "--no-tags", "origin", timeout=180)
            after = self._remote_ref_map()
        except DesktopBackendError:
            self._record("fetch", "error", f"Repository {self.repository}: Git fetch failed; raw Git output was suppressed.")
            raise DesktopBackendError(
                "git_fetch_failed", "Git fetch failed. Check the origin and configured Git credential helper.") from None
        added = set(after) - set(before)
        updated = {name for name in set(after) & set(before) if after[name] != before[name]}
        self.branches = [{"name": name, "sha": sha}
                         for name, sha in sorted(after.items(), key=lambda item: item[0].casefold())]
        self.fetched = True
        details = [f"Repository {self.repository}: fetched {len(after)} remote branches; "
                   f"{len(added)} new and {len(updated)} updated."]
        details.extend("  + origin/" + github_api.redact_text(name, 255)
                       for name in sorted(added, key=str.casefold))
        details.extend("  ~ origin/" + github_api.redact_text(name, 255)
                       for name in sorted(updated, key=str.casefold))
        self._record("fetch", "ok", "\n".join(details))
        return {"branches": self.branches, "count": len(after),
                "added": len(added), "updated": len(updated), "tags_fresh": False}

    def fetch_remote_tags(self):
        if not self.repository_path or not self.repository or not self.fetched:
            raise DesktopBackendError("fetch_required", "Fetch remote branches before loading tags.")
        before_tags = self._tag_ref_map("refs/remotes/origin-tags")
        try:
            # Keep remote tags in an application-owned remote-tracking namespace.
            # This avoids overwriting conflicting user-created refs/tags.
            self._fetch_output("fetch", "--no-recurse-submodules", "--no-tags", "--prune", "origin",
                               "+refs/tags/*:refs/remotes/origin-tags/*", timeout=180)
            after_tags = self._tag_ref_map("refs/remotes/origin-tags")
            self.tags_fresh = True
            new_tags = set(after_tags) - set(before_tags)
            updated_tags = {name for name in set(after_tags) & set(before_tags)
                            if after_tags[name] != before_tags[name]}
            self._record("fetch/tags", "ok",
                         f"Fetched {len(after_tags)} tags; {len(new_tags)} new and {len(updated_tags)} updated.")
        except DesktopBackendError:
            self.tags_fresh = False
            self._record("fetch/tags", "warning",
                         "Remote tag fetch failed; existing local tags were not force-updated.")
            raise DesktopBackendError(
                "tag_sync_failed", "Could not fetch remote tags. Check origin and the Git credential helper.") from None
        return {"count": len(after_tags), "added": len(new_tags), "updated": len(updated_tags)}

    def _source_commit_log(self, source):
        source_ref = f"refs/remotes/origin/{source}"
        output = _git_output(
            self.repository_path, "log", "--no-color", "--no-show-signature", "-z",
            "--max-count=101", "--format=%H%x00%aI%x00%an%x00%s", source_ref, timeout=20)
        fields = output.split("\x00")
        if fields and fields[-1] == "":
            fields.pop()
        if len(fields) % 4:
            raise DesktopBackendError("commit_log_invalid", "Git returned an invalid source commit list.")
        commits = []
        for index in range(0, len(fields), 4):
            sha, authored_at, author, subject = fields[index:index + 4]
            if not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
                raise DesktopBackendError("commit_log_invalid", "Git returned an invalid source commit list.")
            try:
                epoch = datetime.fromisoformat(authored_at).timestamp()
            except (ValueError, OverflowError, OSError):
                raise DesktopBackendError("commit_log_invalid", "Git returned an invalid author date.") from None
            commits.append({"sha": sha, "authored_at": authored_at,
                            "author": github_api.redact_text(author, 200),
                            "subject": github_api.redact_text(subject, 500), "_epoch": epoch})
        commits.sort(key=lambda item: -item["_epoch"])
        return [{key: value for key, value in item.items() if key != "_epoch"}
                for item in commits[:100]], len(commits) > 100

    def source_commit_detail(self, sha):
        if (not self.fetched or not self.comparison or not isinstance(sha, str)
                or not re.fullmatch(r"[0-9a-fA-F]{40}", sha)
                or sha not in {item["sha"] for item in self.comparison.get("source_commits", [])}):
            raise DesktopBackendError("commit_unverified", "Select a commit from the current source history.")
        message = _git_output(self.repository_path, "show", "--no-color", "--no-show-signature",
                              "--no-patch", "--format=%B", sha, "--", timeout=20)
        safe_message = github_api.redact_text(message)
        limit = 100_000
        return {"sha": sha, "message": safe_message[:limit], "truncated": len(safe_message) > limit}

    def list_branch_tags(self):
        if not self.fetched:
            raise DesktopBackendError("fetch_required", "Fetch origin before listing branch tags.")
        if not self.tags_fresh:
            self.fetch_remote_tags()
        branch_tips = {item["name"]: item["sha"] for item in self.branches}
        try:
            remote_tags = self._tag_ref_map("refs/remotes/origin-tags")
            # Send each tag once across IPC; large repositories can have tens of
            # thousands of branch/tag associations. The GUI expands the indexes.
            sorted_tags = sorted(remote_tags.values(),
                                 key=lambda tag: (-tag["created_epoch"], tag["name"].casefold()))
            branches_by_name = {
                name: {"name": name, "sha": sha, "tag_indexes": []}
                for name, sha in branch_tips.items()
            }
            tag_targets = {}
            for index, tag in enumerate(sorted_tags):
                tag_targets.setdefault(tag["sha"], []).append(index)
            for target_sha, tag_indexes in tag_targets.items():
                output = _git_output(
                    self.repository_path, "for-each-ref", f"--contains={target_sha}",
                    "--format=%(refname)", "refs/remotes/origin")
                reachable = {
                    ref[len("refs/remotes/origin/"):]
                    for ref in output.splitlines()
                    if ref.startswith("refs/remotes/origin/") and ref != "refs/remotes/origin/HEAD"
                }
                for branch in reachable & branches_by_name.keys():
                    branches_by_name[branch]["tag_indexes"].extend(tag_indexes)
        except DesktopBackendError:
            raise DesktopBackendError("tag_list_unavailable", "Could not list remote tags by branch.") from None
        branches = list(branches_by_name.values())
        for branch in branches:
            branch["tag_indexes"].sort()
        branches.sort(key=lambda branch: (
            not bool(branch["tag_indexes"]),
            -sorted_tags[branch["tag_indexes"][0]]["created_epoch"] if branch["tag_indexes"] else 0,
            branch["name"].casefold(),
        ))
        tags = [{key: value for key, value in tag.items() if key != "created_epoch"}
                for tag in sorted_tags]
        self._record("tags/list", "ok", "Mapped remote tags to fetched branches.")
        return {"branches": branches, "tags": tags, "branch_count": len(branches),
                "tagged_branch_count": sum(bool(branch["tag_indexes"]) for branch in branches)}

    def _origin_tag_targets(self):
        try:
            output = self._fetch_output("ls-remote", "--tags", "origin", timeout=60)
        except DesktopBackendError:
            raise DesktopBackendError("workflow_tags_unavailable", "Could not read origin tags for workflow matching.") from None
        direct, peeled = {}, {}
        for line in output.splitlines():
            sha, separator, ref = line.partition("\t")
            if not separator or not re.fullmatch(r"[0-9a-fA-F]{40}", sha) or not ref.startswith("refs/tags/"):
                continue
            name = ref[len("refs/tags/"):]
            is_peeled = name.endswith("^{}")
            if is_peeled:
                name = name[:-3]
            if not github_api._branch_valid(name):
                continue
            (peeled if is_peeled else direct)[name] = sha.lower()
        return {name: peeled.get(name, sha) for name, sha in direct.items()}

    def _workflow_branch_associations(self, sha):
        if not self.fetched:
            return {"available": False, "exact": [], "related": [], "exact_count": 0, "related_count": 0}
        try:
            output = _git_output(self.repository_path, "for-each-ref", f"--contains={sha}",
                                 "--format=%(refname)", "refs/remotes/origin", timeout=20)
        except DesktopBackendError:
            return {"available": False, "exact": [], "related": [], "exact_count": 0, "related_count": 0}
        names = {item["name"]: item["sha"] for item in self.branches}
        reachable = {ref[len("refs/remotes/origin/"):] for ref in output.splitlines()
                     if ref.startswith("refs/remotes/origin/")}
        exact = sorted((name for name in reachable & names.keys() if names[name].lower() == sha.lower()),
                       key=str.casefold)
        related = sorted((name for name in reachable & names.keys() if names[name].lower() != sha.lower()),
                         key=str.casefold)
        return {"available": True, "exact": exact[:100], "related": related[:100],
                "exact_count": len(exact), "related_count": len(related)}

    def list_workflow_tag_runs(self, *, reset=False, workflow_name=None):
        api = self._api()
        workflow_name = workflow_name if isinstance(workflow_name, str) and workflow_name.strip() else "build-and-deploy"
        workflow_name = workflow_name.strip()[:100]
        if reset or self.workflow_state is None:
            self.workflow_state = None
            state = {"workflow_name": workflow_name,
                     "tags": self._origin_tag_targets(), "next_page": 1,
                     "pending": [], "seen": set(), "scan_limited": False}
        else:
            previous = self.workflow_state
            state = {**previous, "pending": list(previous["pending"]), "seen": set(previous["seen"])}
        candidates = []
        warning = ""
        while len(candidates) < 20:
            if not state["pending"]:
                if state["next_page"] is None:
                    break
                try:
                    page = api.workflow_runs(self.repository, state["next_page"],
                                             workflow_name=state["workflow_name"])
                except github_api.APIError as error:
                    if error.code == "github_auth_failed" or not candidates:
                        self._raise_api_error(error)
                    warning = "Some workflow history could not be loaded; retry Load more."
                    break
                state["pending"] = page["runs"]
                state["next_page"] = page["next_page"]
                state["scan_limited"] = state["scan_limited"] or page["scan_limited"]
                if not state["pending"]:
                    continue
            run = state["pending"].pop(0)
            tag = run["ref"]
            if tag in state["seen"] or state["tags"].get(tag) != run["sha"].lower():
                continue
            state["seen"].add(tag)
            candidates.append(run)

        stop_jobs = threading.Event()
        def load_details(run):
            jobs, error = [], None
            skipped = stop_jobs.is_set()
            if not skipped:
                try:
                    jobs = api.workflow_run_jobs(self.repository, run["id"])
                except github_api.APIError as caught:
                    error = caught
                    if caught.code in ("github_auth_failed", "github_rate_limit", "github_unavailable", "github_operation_timeout"):
                        stop_jobs.set()
            jobs_error = "Deployment jobs unavailable; refresh to retry." if error or skipped else ""
            return ({**run, "tag": run["ref"], "jobs": jobs, "jobs_error": jobs_error,
                     "branches": self._workflow_branch_associations(run["sha"])}, error)

        with ThreadPoolExecutor(max_workers=5) as executor:
            results = list(executor.map(load_details, candidates))
        for _row, error in results:
            if error and error.code == "github_auth_failed":
                self._raise_api_error(error)
        rows = [row for row, _error in results]
        self.workflow_state = state
        self._record("workflow/list", "ok" if not warning else "incomplete",
                     f"Loaded {len(rows)} latest tag-matching workflow runs.")
        return {"runs": rows, "has_more": bool(state["pending"] or state["next_page"]),
                "scan_limited": state["scan_limited"], "warning": warning}

    def suggest_tag_name(self, branch):
        if not self.fetched or branch not in {item["name"] for item in self.branches}:
            raise DesktopBackendError("branch_unverified", "Fetch origin and select a remote branch first.")
        rules = self.load_settings().get("tag_rules", {})
        rule = next((item for item in rules.get("suggestions", []) if item["branch"] == branch), None)
        if rule is None:
            return {"suggestion": "", "matched_count": 0}
        prefix, suffix = rule["prefix"], rule["suffix"]
        version_pattern = re.compile(re.escape(prefix) + r"([0-9]+)" + re.escape(suffix))
        try:
            output = self._fetch_output("ls-remote", "--tags", "--refs", "origin", timeout=60)
        except DesktopBackendError:
            raise DesktopBackendError(
                "tag_suggestion_unavailable", "Could not check origin tags for a suggested version; enter a name manually.") from None
        versions = []
        for line in output.splitlines():
            _sha, separator, ref = line.partition("\t")
            if not separator or not ref.startswith("refs/tags/"):
                continue
            name = ref[len("refs/tags/"):]
            if len(name) > 255:
                continue
            match = version_pattern.fullmatch(name)
            if match:
                versions.append(int(match.group(1)))
        suggestion = f"{prefix}{max(versions) + 1}{suffix}" if versions else ""
        if suggestion and not github_api._branch_valid(suggestion):
            raise DesktopBackendError("tag_suggestion_unavailable", "The next suggested version is not a valid Git tag name.")
        self._record("tags/suggest", "ok", "Checked origin tags for the configured version suggestion.")
        return {"suggestion": suggestion, "matched_count": len(versions)}

    def create_remote_tag(self, branch, tag_name, *, confirmed=False):
        if confirmed is not True:
            raise DesktopBackendError("confirmation_required", "Review and confirm the remote tag before creating it.")
        if not self.fetched or branch not in {item["name"] for item in self.branches}:
            raise DesktopBackendError("branch_unverified", "Fetch origin and select a remote branch first.")
        if not github_api._branch_valid(tag_name):
            raise DesktopBackendError("invalid_tag", "Enter a valid tag name.")
        branch_info = next(item for item in self.branches if item["name"] == branch)
        api = self._api()
        try:
            current = api.branch_tip(self.repository, branch)
            if current["sha"] != branch_info["sha"]:
                self.fetched = False
                raise DesktopBackendError("branch_changed", "The selected branch moved; Fetch again before creating a tag.")
            created = api.create_tag(self.repository, tag_name, current["sha"])
        except github_api.APIError as error:
            if error.code == "tag_exists":
                raise DesktopBackendError("tag_exists", "That tag already exists on GitHub; nothing was changed.") from None
            self._raise_api_error(error)
        self._record("tag/create", "ok", "Remote tag created at the selected branch tip.")
        self.tags_fresh = False
        self.workflow_state = None
        return {**created, "branch": branch}

    def compare(self, base, source):
        if not self.fetched:
            raise DesktopBackendError("fetch_required", "Fetch origin before comparing remote branches.")
        if base not in {item["name"] for item in self.branches} or source not in {item["name"] for item in self.branches}:
            raise DesktopBackendError("branch_unverified", "Choose both branches from the current remote branch list.")
        if base == source:
            raise DesktopBackendError("invalid_branch", "Base and source branches must be different.")
        try:
            result = self._api().compare(self.repository, base, source)
        except github_api.APIError as error:
            self._raise_api_error(error)
        result["default_title"] = github_api.build_default_title(result.get("commits", []), base, source)
        result["default_body"] = github_api.build_default_body(
            result.get("commits", []), result.get("files", []), base, source,
            result.get("base_sha", ""), result.get("source_sha", ""),
            truncated=not result.get("complete", False))
        result["source_commits"], result["source_commits_truncated"] = self._source_commit_log(source)
        self.comparison = result
        self.inspection = None
        self.reviewer_candidates.clear()
        self.copilot_reviewer_available = False
        self._record("compare", "ok" if result.get("complete") else "incomplete",
                     f"Remote comparison returned {len(result.get('files', []))} files and "
                     f"{len(result.get('commits', []))} commits.")
        return result

    def search_reviewers(self, query):
        if not isinstance(query, str) or not query.strip():
            raise DesktopBackendError("invalid_query", "Enter a GitHub login or name to search.")
        if "copilot" in query.casefold():
            special = {
                "login": COPILOT_REVIEWER_ALIAS,
                "name": "GitHub Copilot code review (special reviewer)",
                "kind": "copilot",
            }
            self.reviewer_candidates.add(special["login"])
            self.copilot_reviewer_available = True
            self._record("reviewers/search", "ok", "Copilot code-review option displayed as a special candidate.")
            return {"reviewers": [special], "next_cursor": None, "complete": True}
        try:
            result = self._api().reviewers(self.repository, query.strip())
        except github_api.APIError as error:
            self._raise_api_error(error)
        self.reviewer_candidates.update(item["login"] for item in result.get("reviewers", []))
        self._record("reviewers/search", "ok",
                     f"Returned {len(result.get('reviewers', []))} assignable reviewers.")
        return result

    def load_settings(self):
        path = preferences_store.install_path(__file__)
        try:
            settings = preferences_store.read_preferences(path)
        except preferences_store.PreferencesError as error:
            if error.code == "github_app_unconfigured":
                return {"draft": {}, "branch_selection": None, "tag_rules": {}}
            raise DesktopBackendError(error.code, str(error)) from None
        draft = settings.get("desktop_pr_workflow", {})
        selection = next((item for item in settings.get("desktop_branch_selections", [])
                          if self.repository and item["repository"].casefold() == self.repository.casefold()), None)
        return {"draft": draft if isinstance(draft, dict) else {}, "branch_selection": selection,
                "tag_rules": settings.get("desktop_tag_rules", {})}

    def remember_branch_selection(self, base, source):
        if not self.repository or not self.fetched:
            raise DesktopBackendError("fetch_required", "Fetch the selected repository before saving its branches.")
        names = {item["name"] for item in self.branches}
        for branch in (base, source):
            if not isinstance(branch, str) or (branch and (
                    not github_api._branch_valid(branch) or branch not in names)):
                raise DesktopBackendError("branch_unverified", "Choose branches from the fetched origin refs.")
            if github_api.contains_secret(branch):
                raise DesktopBackendError("preferences_invalid", "A branch name contains credential-like content and was not saved.")
        selection = {"repository": self.repository, "base_branch": base, "source_branch": source}
        try:
            preferences_store.save_preferences({"desktop_branch_selections": [selection]},
                                               preferences_store.install_path(__file__))
        except preferences_store.PreferencesError as error:
            self._record("settings/branches", "warning", "Could not save the selected branches.")
            raise DesktopBackendError(error.code, str(error)) from None
        self._record("settings/branches", "ok", "Selected branches saved locally for this repository.")
        return {"saved": True}

    def save_settings(self, draft):
        if not isinstance(draft, dict):
            raise DesktopBackendError("preferences_invalid", "The saved PR settings are invalid.")
        allowed = {"repository", "base_branch", "source_branch", "title", "body",
                   "reviewers", "local_repository_path"}
        if set(draft) - allowed:
            raise DesktopBackendError("preferences_invalid", "The saved PR settings contain unsupported fields.")
        if (any(not isinstance(draft.get(key, ""), str) or len(draft.get(key, "")) > limit
                for key, limit in (("repository", 200), ("base_branch", 255),
                                   ("source_branch", 255), ("title", 256), ("body", 60000),
                                   ("local_repository_path", 4096)))
                or not isinstance(draft.get("reviewers", []), list)
                or len(draft.get("reviewers", [])) > 50
                or any(not isinstance(login, str) or len(login) > 39
                       for login in draft.get("reviewers", []))):
            raise DesktopBackendError("preferences_invalid", "The saved PR settings contain invalid values.")
        if github_api.contains_secret(draft.get("title", "")) or github_api.contains_secret(draft.get("body", "")):
            raise DesktopBackendError("content_contains_secret", "Remove credential-like content before saving the draft.")
        path = preferences_store.install_path(__file__)
        try:
            preferences_store.save_preferences({"desktop_pr_workflow": draft}, path)
            saved = preferences_store.read_preferences(path)
        except preferences_store.PreferencesError as error:
            raise DesktopBackendError(error.code, str(error)) from None
        self._record("settings/save", "ok", "Local PR settings saved without credentials.")
        return {"draft": saved.get("desktop_pr_workflow", {})}

    def remember_local_repository_path(self, path):
        if not isinstance(path, str) or not path.strip() or len(path) > 4096:
            raise DesktopBackendError("preferences_invalid", "The selected local directory path is invalid.")
        preferences_path = preferences_store.install_path(__file__)
        try:
            try:
                settings = preferences_store.read_preferences(preferences_path)
            except preferences_store.PreferencesError as error:
                if error.code != "github_app_unconfigured":
                    raise
                settings = {}
            workflow = settings.get("desktop_pr_workflow", {})
            if not isinstance(workflow, dict):
                workflow = {}
            workflow = {**workflow, "local_repository_path": path}
            preferences_store.save_preferences(
                {"desktop_pr_workflow": workflow}, preferences_path)
        except preferences_store.PreferencesError as error:
            raise DesktopBackendError(error.code, str(error)) from None
        self._record("settings/save", "ok", "Last selected directory saved locally.")
        return {"saved": True}

    def inspect(self, base, source):
        comparison = self.comparison
        if not comparison or comparison.get("base") != base or comparison.get("source") != source:
            raise DesktopBackendError("comparison_stale", "Compare the selected remote branches before inspecting PRs.")
        try:
            result = self._api().inspect(self.repository, base, source)
        except github_api.APIError as error:
            self._raise_api_error(error)
        self.inspection = {"base": base, "source": source, **result}
        self._record("pull-requests/inspect", "ok",
                     "Matching open PR found." if result.get("exists") else "No matching open PR found.")
        return result

    def create(self, base, source, title, body, reviewers, *, confirmed=False):
        if confirmed is not True:
            raise DesktopBackendError("confirmation_required", "Review and confirm the pull request before creating it.")
        comparison = self.comparison
        inspection = self.inspection
        if (not comparison or comparison.get("base") != base or comparison.get("source") != source
                or not comparison.get("complete")):
            raise DesktopBackendError("comparison_stale", "Run a complete remote comparison before creating a PR.")
        if not inspection or inspection.get("base") != base or inspection.get("source") != source:
            raise DesktopBackendError("inspection_required", "Inspect for an existing PR before creating one.")
        if inspection.get("exists"):
            self._record("pull-requests/create", "skipped", "A matching open PR already exists.")
            return {"created": False, "existing": inspection.get("pull_request")}
        if (not isinstance(reviewers, list) or len(reviewers) > 50
                or any(login not in self.reviewer_candidates for login in reviewers)):
            raise DesktopBackendError("reviewers_unverified", "Select reviewers returned by the repository-scoped search.")
        copilot_login = COPILOT_REVIEWER_ALIAS
        copilot_requested = copilot_login in reviewers
        if copilot_requested and not self.copilot_reviewer_available:
            raise DesktopBackendError("reviewers_unverified", "Search for Copilot again before requesting its review.")
        if github_api.contains_secret(title) or github_api.contains_secret(body):
            raise DesktopBackendError("content_contains_secret", "Remove credential-like content before creating a PR.")
        api = self._api()
        regular_reviewers = [login for login in reviewers if login != copilot_login]
        for login in regular_reviewers:
            try:
                result = api.reviewers(self.repository, login)
            except github_api.APIError as error:
                self._raise_api_error(error)
            if login not in {item["login"] for item in result.get("reviewers", [])}:
                raise DesktopBackendError("reviewer_validation_failed", "Recheck the reviewer for this repository.")
        try:
            result = api.create(
                self.repository, base, source, title, body, regular_reviewers,
                comparison["base_sha"], comparison["source_sha"])
        except github_api.APIError as error:
            # A failed/uncertain write requires a fresh read-only inspection
            # before another explicit attempt can be enabled.
            self.inspection = None
            self._record("pull-requests/create", "error", "GitHub did not confirm PR creation; inspect before retrying.")
            self._raise_api_error(error)
        self.inspection = None
        if copilot_requested and result.get("created") and result.get("pull_request"):
            pull_number = result["pull_request"].get("number")
            try:
                result["copilot_review"] = api.request_copilot_review(self.repository, pull_number)
            except github_api.APIError as error:
                if error.code == "github_auth_failed":
                    self.sign_out()
                result["copilot_review"] = {
                    "requested": True, "confirmed": False, "error_code": error.code,
                }
        self._record("pull-requests/create", "ok" if result.get("created") else "skipped",
                     "Pull request created." if result.get("created") else "No pull request was created.")
        return result
