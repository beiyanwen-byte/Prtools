"""Protocol tests; no GitHub request leaves the test process.

Most payloads are hand-built. ``fixtures/github/`` contains synthetic responses
that preserve important GitHub API response shapes.
"""

import io
import json
import unittest
from pathlib import Path
from urllib.error import URLError
from unittest.mock import patch

import github_api

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class FakeResponse:
    def __init__(self, value, headers=None):
        self.payload = value if isinstance(value, bytes) else json.dumps(value).encode("utf-8")
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, size=-1):
        return self.payload if size < 0 else self.payload[:size]


class GitHubAPITest(unittest.TestCase):
    TOKEN = "synthetic-server-token"

    def test_workflow_name_can_select_build_and_push_or_build_and_deploy(self):
        api = github_api.GitHubAPI(self.TOKEN)
        with patch.object(api, "_get", return_value=({"workflow_runs": []}, {})) as get:
            result = api.workflow_runs("owner/repo", 1, workflow_name="build-and-push")
        self.assertEqual([], result["runs"])
        self.assertIn("/build-and-push.yaml/runs", get.call_args.args[0])
        with patch.object(api, "_get", return_value=({"workflow_runs": []}, {})) as get:
            api.workflow_runs("owner/repo", 1, workflow_name="build-and-deploy.yaml")
        self.assertIn("/build-and-deploy.yaml/runs", get.call_args.args[0])

    def test_workflow_runs_are_read_only_and_validate_pagination(self):
        api = github_api.GitHubAPI(self.TOKEN)
        payload = {"workflow_runs": [
            {"id": 123, "run_number": 7, "run_attempt": 2, "head_branch": "v1.0.1",
             "head_sha": "a" * 40, "event": "push", "status": "completed",
             "conclusion": "success", "created_at": "2026-01-02T03:04:05Z",
             "actor": {"login": "synthetic-user"}, "html_url": "https://evil.invalid/"},
            {"id": 124, "run_number": 8, "head_branch": None, "head_sha": "b" * 40},
        ]}
        link = '<https://api.github.com/repos/owner/repo/actions/workflows/build-and-deploy.yaml/runs?per_page=100&page=2>; rel="next"'
        with patch.object(api, "_get", return_value=(payload, {"Link": link})) as get:
            result = api.workflow_runs("owner/repo", 1)
        self.assertEqual(2, result["next_page"])
        self.assertEqual(1, len(result["runs"]))
        self.assertEqual("https://github.com/owner/repo/actions/runs/123", result["runs"][0]["url"])
        self.assertEqual("v1.0.1", result["runs"][0]["ref"])
        self.assertEqual("/repos/owner/repo/actions/workflows/build-and-deploy.yaml/runs?per_page=100&page=1",
                         get.call_args.args[0])
        self.assertFalse(get.call_args.kwargs.get("write", False))

    def test_workflow_jobs_identify_environment_and_do_not_claim_unknown_success(self):
        api = github_api.GitHubAPI(self.TOKEN)
        payload = {"jobs": [
            {"name": "build / build", "status": "completed", "conclusion": "success"},
            {"name": "dev (cluster/d1) / deploy", "status": "completed", "conclusion": "success",
             "steps": [{"name": "Sync the Argo CD application and wait for a healthy state",
                        "status": "completed", "conclusion": "success"}]},
            {"name": "dev (cluster/d2) / deploy", "status": "waiting", "conclusion": None},
            {"name": "test (cluster/t1) / deploy", "status": "completed", "conclusion": "failure"},
            {"name": "Deploy application", "status": "in_progress", "conclusion": None},
        ]}
        with patch.object(api, "_get", return_value=(payload, {})) as get:
            jobs = api.workflow_run_jobs("owner/repo", 123)
        self.assertEqual(["cluster/d1", "cluster/d2", "cluster/t1", ""],
                         [item["environment"] for item in jobs])
        self.assertEqual(["success", "waiting", "failure", "in_progress"],
                         [item["state"] for item in jobs])
        self.assertEqual("Deploy application", jobs[-1]["job_name"])
        self.assertTrue(jobs[0]["sync_confirmed"])
        self.assertFalse(jobs[-1]["sync_confirmed"])
        self.assertFalse(get.call_args.kwargs.get("write", False))

    def test_device_flow_start_validates_fixed_verification_uri_and_response(self):
        good = {
            "device_code": "synthetic-device-code", "user_code": "ABCD-EFGH",
            "verification_uri": github_api.VERIFY_URL, "expires_in": 600, "interval": 5,
        }

        class Opener:
            def __init__(self):
                self.request = None

            def open(self, request, timeout):
                self.request = request
                return FakeResponse(good)

        opener = Opener()
        with patch.object(github_api, "_opener", return_value=opener):
            result = github_api.begin_device_flow("synthetic-public-client-id")
        self.assertEqual("ABCD-EFGH", result["user_code"])
        self.assertEqual("synthetic-device-code", result["device_code"])
        self.assertIn(b"client_id=synthetic-public-client-id", opener.request.data)
        self.assertNotIn("device_code", opener.request.full_url)

        for uri in ("http://example.invalid/", "https://github.com.evil.invalid/login/device"):
            bad = dict(good, verification_uri=uri)
            with patch.object(github_api, "_opener", return_value=type("O", (), {
                "open": lambda self, request, timeout, value=bad: FakeResponse(value),
            })()):
                with self.assertRaises(github_api.APIError) as error:
                    github_api.begin_device_flow("synthetic-public-client-id")
                self.assertEqual("github_invalid_response", error.exception.code)

    def test_poll_pending_slowdown_denial_and_expiry_are_terminal_or_bounded(self):
        replies = iter((
            {"error": "authorization_pending"},
            {"error": "slow_down"},
            {"error": "access_denied"},
            {"error": "expired_token"},
        ))

        class Opener:
            def open(self, *_args, **_kwargs):
                return FakeResponse(next(replies))

        with patch.object(github_api, "_opener", return_value=Opener()):
            self.assertEqual({"state": "pending", "interval": 5},
                             github_api.poll_device_flow("synthetic-client-id", "synthetic-device", 5))
            self.assertEqual({"state": "pending", "interval": 10},
                             github_api.poll_device_flow("synthetic-client-id", "synthetic-device", 5))
            with self.assertRaises(github_api.APIError) as denied:
                github_api.poll_device_flow("synthetic-client-id", "synthetic-device", 10)
            self.assertEqual("github_auth_denied", denied.exception.code)
            with self.assertRaises(github_api.APIError) as expired:
                github_api.poll_device_flow("synthetic-client-id", "synthetic-device", 10)
            self.assertEqual("github_auth_expired", expired.exception.code)

    def test_remote_identity_validation_and_token_is_header_only(self):
        observed = []

        class Opener:
            def open(self, request, timeout):
                observed.append((request.full_url, request.headers))
                return FakeResponse({"login": "synthetic-account"})

        with patch.object(github_api, "_opener", return_value=Opener()):
            login = github_api.verify_identity(self.TOKEN)
        self.assertEqual("synthetic-account", login)
        url, headers = observed[0]
        self.assertNotIn(self.TOKEN, url)
        self.assertEqual("Bearer " + self.TOKEN, headers["Authorization"])

    def test_remote_identity_accepts_enterprise_managed_login_with_shortcode(self):
        class Opener:
            def open(self, *_args, **_kwargs):
                return FakeResponse({"login": "example-user_enterprise"})

        with patch.object(github_api, "_opener", return_value=Opener()):
            self.assertEqual("example-user_enterprise", github_api.verify_identity(self.TOKEN))

    def test_pages_are_validated_for_host_path_sequence_and_parameters(self):
        valid = {"Link": '<https://api.github.com/user/repos?per_page=100&page=2>; rel="next"'}
        self.assertEqual(2, github_api._next_page(valid, 1, "/user/repos"))
        with self.assertRaises(github_api.APIError):
            github_api._next_page(
                {"Link": '<https://evil.invalid/user/repos?per_page=100&page=2>; rel="next"'},
                1, "/user/repos",
            )
        with self.assertRaises(github_api.APIError):
            github_api._next_page(
                {"Link": '<https://api.github.com/user/repos?per_page=100&page=3>; rel="next"'},
                1, "/user/repos",
            )
        filtered = {"Link": '<https://api.github.com/repos/o/r/pulls?state=open&base=main&per_page=100&page=2>; rel="next"'}
        self.assertEqual(2, github_api._next_page(
            filtered, 1, "/repos/o/r/pulls", {"state": ["open"], "base": ["main"]}))
        canonical_link = {
            "Link": '<https://api.github.com/repositories/123/branches?per_page=100&page=2>; rel="next"'
        }
        self.assertEqual(2, github_api._next_page(
            canonical_link, 1, "/repos/o/r/branches", repository_id=123))
        with self.assertRaises(github_api.APIError):
            github_api._next_page(canonical_link, 1, "/repos/o/r/branches", repository_id=456)

    def test_repository_branch_and_compare_shapes_are_remote_only(self):
        api = github_api.GitHubAPI(self.TOKEN)
        with patch.object(github_api, "_request", return_value=(
            [{"name": "sample", "private": True, "owner": {"login": "sample-owner"}}], {}
        )):
            self.assertEqual("sample-owner/sample", api.repositories(1)["repositories"][0]["full_name"])
        with patch.object(github_api, "_request", return_value=(
            [{"name": "main", "commit": {"sha": "a" * 40}}], {}
        )):
            self.assertEqual("main", api.branches("sample-owner/sample")["branches"][0]["name"])
        with self.assertRaises(github_api.APIError):
            api.branches("../untrusted")
        self.assertEqual(("example-user_enterprise", "sample-repo"),
                         github_api._repo_parts("example-user_enterprise/sample-repo"))

        branch_result = {"branches": [
            {"name": "main", "sha": "a" * 40}, {"name": "feature", "sha": "b" * 40},
        ], "complete": True, "next_page": None}
        compare_result = {
            "status": "ahead", "ahead_by": 1, "behind_by": 0, "total_commits": 1,
            "base_commit": {"sha": "a" * 40}, "head_commit": {"sha": "b" * 40},
            "commits": [{"sha": "c" * 40, "commit": {"message": "Synthetic commit"}}],
            "files": [{"filename": "src/example.py", "status": "modified",
                       "additions": 1, "deletions": 0, "changes": 1,
                       "patch": "@@ -0,0 +1 @@\n+synthetic\n"}],
        }
        with patch.object(api, "branches", return_value=branch_result), \
                patch.object(api, "_get", return_value=(compare_result, {})):
            result = api.compare("sample-owner/sample", "main", "feature")
        self.assertTrue(result["complete"])
        self.assertEqual("available", result["files"][0]["patch_status"])
        self.assertEqual("a" * 40, result["base_sha"])

    def test_branch_pagination_accepts_github_repository_id_link_only_for_same_repo(self):
        api = github_api.GitHubAPI(self.TOKEN)
        branch_payload = [{"name": "main", "commit": {"sha": "a" * 40}}]
        canonical = {"Link": '<https://api.github.com/repositories/123/branches?per_page=100&page=2>; rel="next"'}
        with patch.object(api, "_get", side_effect=[
                (branch_payload, canonical), ({"id": 123}, {})]):
            result = api.branches("sample-owner/sample")
        self.assertEqual(2, result["next_page"])

        api = github_api.GitHubAPI(self.TOKEN)
        with patch.object(api, "_get", side_effect=[
                (branch_payload, canonical), ({"id": 456}, {})]):
            with self.assertRaises(github_api.APIError) as caught:
                api.branches("sample-owner/sample")
        self.assertEqual("pagination_invalid", caught.exception.code)

    def test_compare_accepts_response_without_head_commit(self):
        """Synthetic regression fixture models GitHub omitting head_commit."""
        document = json.loads((FIXTURES / "github" / "compare_main_dev.json").read_text("utf-8"))
        comparison = document["payload"]
        self.assertNotIn("head_commit", comparison)
        base_sha = comparison["base_commit"]["sha"]
        source_sha = comparison["commits"][0]["sha"]
        branches = {"branches": [
            {"name": "main", "sha": base_sha}, {"name": "dev", "sha": source_sha},
        ], "complete": True, "next_page": None}
        api = github_api.GitHubAPI(self.TOKEN)
        with patch.object(api, "branches", return_value=branches), \
                patch.object(api, "_get", return_value=(comparison, {})):
            result = api.compare("sample-owner/sample", "main", "dev")
        self.assertTrue(result["complete"])
        self.assertEqual(base_sha, result["base_sha"])
        self.assertEqual(source_sha, result["source_sha"])
        self.assertTrue(result["files"])
        self.assertTrue(result["commits"])

    def test_compare_still_rejects_a_moved_branch_without_head_commit(self):
        branches = {"branches": [
            {"name": "main", "sha": "a" * 40}, {"name": "feature", "sha": "b" * 40},
        ], "complete": True, "next_page": None}
        comparison = {
            "status": "ahead", "ahead_by": 1, "behind_by": 0, "total_commits": 1,
            "base_commit": {"sha": "a" * 40},
            "commits": [{"sha": "c" * 40, "commit": {"message": "Different tip"}}],
            "files": [],
        }
        api = github_api.GitHubAPI(self.TOKEN)
        with patch.object(api, "branches", return_value=branches), \
                patch.object(api, "_get", return_value=(comparison, {})):
            with self.assertRaises(github_api.APIError) as caught:
                api.compare("sample-owner/sample", "main", "feature")
        self.assertEqual("branch_changed", caught.exception.code)

    def test_redaction_removes_credentials_from_remote_text(self):
        samples = [
            ("Authorization: Bearer " + "ghp_" + "abcdefghijklmnopqrstuvwxyz012345", "ghp_"),
            ("token = " + "gho_" + "abcdefghijklmnopqrstuvwxyz012345", "gho_"),
            ("password: hunter2secret", "hunter2secret"),
            ("https://user:secretpass@example.com/path", "secretpass"),
            ("-----BEGIN RSA " + "PRIVATE KEY-----\nabc\n-----END RSA " + "PRIVATE KEY-----", "PRIVATE KEY"),
        ]
        for sample, marker in samples:
            with self.subTest(sample=sample[:24]):
                self.assertTrue(github_api.contains_secret(sample))
                cleaned = github_api.redact_text(sample)
                self.assertNotIn(marker, cleaned)
                self.assertIn(github_api.REDACTED, cleaned)
                self.assertFalse(github_api.contains_secret(cleaned))
        self.assertFalse(github_api.contains_secret("A normal commit subject"))
        self.assertFalse(github_api.contains_secret(""))

    def test_default_title_and_body_come_only_from_remote_comparison(self):
        commits = [{"message": "First remote subject\ndetails"},
                   {"message": "Second remote subject"}]
        files = [{"filename": "src/example.py", "status": "modified",
                  "additions": 3, "deletions": 1}]
        title = github_api.build_default_title(commits, "main", "dev")
        self.assertEqual("First remote subject (+2 commits)", title)
        body = github_api.build_default_body(commits, files, "main", "dev", "a" * 40, "b" * 40)
        self.assertIn("## Summary", body)
        self.assertIn("First remote subject", body)
        self.assertIn("src/example.py (modified) +3/-1", body)
        self.assertIn("`aaaaaaa...bbbbbbb`", body)
        self.assertEqual("Merge dev into main",
                         github_api.build_default_title([], "main", "dev"))
        self.assertNotIn("hunter2", github_api.build_default_body(
            [{"message": "token: " + "ghp_" + "abcdefghijklmnopqrstuvwxyz012345"}], [], "main", "dev", "a" * 40, "b" * 40))

    def test_compare_marks_missing_or_oversized_patch_incomplete_for_creation(self):
        comparison = {
            "status": "ahead", "ahead_by": 1, "behind_by": 0, "total_commits": 1,
            "base_commit": {"sha": "a" * 40}, "head_commit": {"sha": "b" * 40},
            "commits": [{"sha": "c" * 40, "commit": {"message": "Synthetic"}}],
            "files": [],
        }
        branches = {"branches": [
            {"name": "main", "sha": "a" * 40}, {"name": "feature", "sha": "b" * 40},
        ], "complete": True, "next_page": None}
        api = github_api.GitHubAPI(self.TOKEN)
        cases = [
            ({"filename": "missing", "additions": 1, "deletions": 0, "changes": 1}, "unavailable"),
            ({"filename": "binary", "patch": None, "additions": 0, "deletions": 0, "changes": 0}, "unavailable"),
            ({"filename": "large", "patch": "x" * (github_api.MAX_PATCH_BYTES + 1)}, "oversized"),
            ({"filename": "partial", "patch": "@@ -0,0 +1,2 @@\n+one\n",
              "additions": 2, "deletions": 0, "changes": 2}, "unavailable"),
            ({"filename": "capped", "patch": "@@ -0,0 +1 @@\n+one\n",
              "additions": 2, "deletions": 0, "changes": 2}, "unavailable"),
        ]
        for item, reason in cases:
            with self.subTest(reason=reason, filename=item["filename"]), \
                    patch.object(api, "branches", return_value=branches), \
                    patch.object(api, "_get", return_value=(dict(comparison, files=[item]), {})):
                result = api.compare("sample-owner/sample", "main", "feature")
                self.assertFalse(result["complete"])
                self.assertTrue(result["truncated"])
                self.assertEqual(reason, result["files"][0]["patch_status"])
                self.assertIsNone(result["files"][0]["patch"])

    def test_patch_validation_accepts_realistic_hunks_and_rejects_truncation(self):
        item = {"additions": 2, "deletions": 1, "changes": 3}
        patch_text = "@@ -1,2 +1,2 @@ section\n context\n-old\n+new\n@@ -5,0 +6 @@\n+last\n\\ No newline at end of file\n"
        self.assertTrue(github_api._patch_is_complete(patch_text, item))
        self.assertFalse(github_api._patch_is_complete(patch_text.replace("+last\n", ""), item))
        self.assertFalse(github_api._patch_is_complete(patch_text, dict(item, additions=3, changes=4)))

    def test_capped_files_and_incomplete_commits_are_not_complete(self):
        api = github_api.GitHubAPI(self.TOKEN)
        branches = {"branches": [
            {"name": "main", "sha": "a" * 40}, {"name": "feature", "sha": "b" * 40},
        ], "complete": True, "next_page": None}
        file = {"filename": "example", "patch": "@@ -0,0 +1 @@\n+line\n",
                "additions": 1, "deletions": 0, "changes": 1}
        comparison = {
            "status": "ahead", "ahead_by": 1, "behind_by": 0, "total_commits": 1,
            "base_commit": {"sha": "a" * 40}, "head_commit": {"sha": "b" * 40},
            "commits": [{"sha": "c" * 40, "commit": {"message": "Synthetic"}}],
            "files": [file] * github_api.MAX_FILES,
        }
        with patch.object(api, "branches", return_value=branches), \
                patch.object(api, "_get", return_value=(comparison, {})):
            result = api.compare("sample-owner/sample", "main", "feature")
        self.assertFalse(result["complete"])
        self.assertFalse(result["files_complete"])
        with patch.object(api, "branches", return_value=branches), \
                patch.object(api, "_get", return_value=(dict(comparison, files=[file], total_commits=2), {})):
            result = api.compare("sample-owner/sample", "main", "feature")
        self.assertFalse(result["complete"])
        self.assertFalse(result["commits_complete"])

    def test_inspection_follows_pages_before_reporting_no_match(self):
        api = github_api.GitHubAPI(self.TOKEN)
        path = "/repos/sample-owner/sample/pulls"
        link = {"Link": f'<https://api.github.com{path}?state=open&base=main&per_page=100&page=2>; rel="next"'}
        with patch.object(api, "_get", side_effect=[
            ([], link),
            ([{"number": 7, "html_url": "https://github.com/sample-owner/sample/pull/7",
              "title": "Synthetic",
              "base": {"ref": "main", "repo": {"full_name": "sample-owner/sample"}},
              "head": {"ref": "feature", "repo": {"full_name": "sample-owner/sample"}}}], {}),
        ]) as get:
            result = api.inspect("sample-owner/sample", "main", "feature")
        self.assertTrue(result["exists"])
        self.assertEqual(7, result["pull_request"]["number"])
        self.assertEqual(2, get.call_count)

    def test_reviewer_search_is_repository_scoped_and_uses_graphql_variables(self):
        api = github_api.GitHubAPI(self.TOKEN)
        payload = {"data": {"repository": {"assignableUsers": {
            "nodes": [{"login": "synthetic-reviewer", "name": "Synthetic Reviewer"}],
            "pageInfo": {"hasNextPage": True, "endCursor": "synthetic-cursor"},
        }}}}
        with patch.object(api, "_get", return_value=(payload, {})) as get:
            result = api.reviewers("sample-owner/sample", "synthetic search", None)
        self.assertEqual("synthetic-cursor", result["next_cursor"])
        self.assertFalse(result["complete"])
        args, kwargs = get.call_args
        self.assertEqual("/graphql", args[0])
        self.assertEqual("sample-owner", args[1]["variables"]["owner"])
        self.assertEqual("sample", args[1]["variables"]["repo"])
        self.assertEqual("synthetic search", args[1]["variables"]["search"])
        self.assertIsNone(kwargs.get("write"))

    def test_reviewer_search_accepts_enterprise_managed_logins(self):
        api = github_api.GitHubAPI(self.TOKEN)
        payload = {"data": {"repository": {"assignableUsers": {
            "nodes": [{"login": "reviewer_example", "name": "Managed Reviewer"}],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }}}}
        with patch.object(api, "_get", return_value=(payload, {})):
            result = api.reviewers("example-owner_enterprise/sample-repo", "reviewer")
        self.assertEqual("reviewer_example", result["reviewers"][0]["login"])

    def test_copilot_review_uses_the_dedicated_github_endpoint(self):
        api = github_api.GitHubAPI(self.TOKEN)
        with patch.object(api, "_get", return_value=({"state": "PENDING"}, {})) as get:
            result = api.request_copilot_review("sample-owner/sample", 17)
        self.assertTrue(result["requested"])
        self.assertTrue(result["confirmed"])
        self.assertEqual("/repos/sample-owner/sample/pulls/17/copilot-code-review", get.call_args.args[0])
        self.assertIsNone(get.call_args.kwargs.get("body"))
        self.assertTrue(get.call_args.kwargs.get("write"))
        self.assertEqual("POST", get.call_args.kwargs.get("method"))

    def test_create_remote_tag_checks_name_and_uses_git_refs_endpoint(self):
        api = github_api.GitHubAPI(self.TOKEN)
        sha = "a" * 40
        with patch.object(api, "_get", side_effect=[
                github_api.APIError("not_found", "missing", 404),
                ({"ref": "refs/tags/release/v1", "object": {"sha": sha}}, {}),
        ]) as get:
            result = api.create_tag("sample-owner/sample", "release/v1", sha)
        self.assertTrue(result["created"])
        self.assertEqual(sha, result["sha"])
        self.assertEqual("/repos/sample-owner/sample/git/ref/tags/release/v1", get.call_args_list[0].args[0])
        self.assertEqual("/repos/sample-owner/sample/git/refs", get.call_args_list[1].args[0])
        self.assertEqual({"ref": "refs/tags/release/v1", "sha": sha}, get.call_args_list[1].args[1])
        self.assertTrue(get.call_args_list[1].kwargs.get("write"))

    def test_create_remote_tag_reports_existing_name_without_post(self):
        api = github_api.GitHubAPI(self.TOKEN)
        with patch.object(api, "_get", return_value=(
                {"ref": "refs/tags/v1.0", "object": {"sha": "a" * 40}}, {})) as get:
            with self.assertRaises(github_api.APIError) as caught:
                api.create_tag("sample-owner/sample", "v1.0", "b" * 40)
        self.assertEqual("tag_exists", caught.exception.code)
        get.assert_called_once()

    def test_create_remote_tag_maps_atomic_name_collision_to_exists(self):
        api = github_api.GitHubAPI(self.TOKEN)
        missing = github_api.APIError("not_found", "missing", 404)
        conflict = github_api.APIError("github_pr_rejected", "rejected", 422)
        with patch.object(api, "_get", side_effect=[missing, conflict]) as get:
            with self.assertRaises(github_api.APIError) as caught:
                api.create_tag("sample-owner/sample", "v1.0", "a" * 40)
        self.assertEqual("tag_exists", caught.exception.code)
        self.assertEqual(2, get.call_count)

    def test_create_remote_tag_rejects_git_invalid_names_before_request(self):
        api = github_api.GitHubAPI(self.TOKEN)
        for tag in ("/leading", "release//nested", "release/.hidden", "release/v1.lock"):
            with self.subTest(tag=tag), patch.object(api, "_get") as get:
                with self.assertRaises(github_api.APIError) as caught:
                    api.create_tag("sample-owner/sample", tag, "a" * 40)
            self.assertEqual("invalid_tag", caught.exception.code)
            get.assert_not_called()

    def test_create_remote_duplicate_is_reported_without_post(self):
        api = github_api.GitHubAPI(self.TOKEN)
        existing = {"exists": True, "pull_request": {
            "number": 7, "url": "https://github.com/sample-owner/sample/pull/7",
            "title": "Synthetic existing",
        }}
        with patch.object(api, "inspect", return_value=existing), \
                patch.object(api, "compare") as compare, patch.object(api, "_get") as get:
            result = api.create("sample-owner/sample", "main", "feature", "Synthetic", "",
                                [], "a" * 40, "b" * 40)
        self.assertFalse(result["created"])
        self.assertTrue(result["existing"]["exists"])
        compare.assert_not_called()
        get.assert_not_called()

    def test_create_rechecks_and_does_not_retry_uncertain_write(self):
        api = github_api.GitHubAPI(self.TOKEN)
        compare = {"complete": True, "base_sha": "a" * 40, "source_sha": "b" * 40,
                   "ahead_by": 1}
        class Opener:
            def open(self, *_args, **_kwargs):
                raise URLError("synthetic network diagnostic")
        with patch.object(api, "inspect", return_value={"exists": False}), \
                patch.object(api, "compare", return_value=compare), \
                patch.object(github_api, "_opener", return_value=Opener()), \
                patch.object(api, "_get", wraps=api._get) as get:
            with self.assertRaises(github_api.APIError) as error:
                api.create("sample-owner/sample", "main", "feature", "Synthetic", "",
                           [], "a" * 40, "b" * 40)
        self.assertEqual("github_write_uncertain", error.exception.code)
        self.assertEqual(1, get.call_count)
        self.assertNotIn("synthetic network diagnostic", str(error.exception))


if __name__ == "__main__":
    unittest.main()
