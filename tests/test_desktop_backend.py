"""Offline tests for the native GUI/backend boundary."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import desktop_backend
import desktop_worker


SYNTHETIC_TAG_RULES = {"branch_filter": "release-*", "suggestions": [
    {"branch": "release-example", "prefix": "v2.5.", "suffix": "-demo"},
]}


class DesktopBackendTests(unittest.TestCase):
    def test_worker_dispatches_source_commit_detail(self):
        backend = Mock()
        backend.source_commit_detail.return_value = {"sha": "a" * 40, "message": "Full message"}
        result = desktop_worker.dispatch(backend, {"action": "source_commit_detail", "sha": "a" * 40})
        backend.source_commit_detail.assert_called_once_with("a" * 40)
        self.assertEqual("Full message", result["message"])

    def test_worker_dispatches_named_workflow_tag_list(self):
        backend = Mock()
        backend.list_workflow_tag_runs.return_value = {"runs": [], "has_more": False}
        result = desktop_worker.dispatch(backend, {"action": "list_workflow_tag_runs",
                                                   "reset": True,
                                                   "workflow_name": "build-and-push"})
        backend.list_workflow_tag_runs.assert_called_once_with(reset=True,
                                                               workflow_name="build-and-push")
        self.assertEqual([], result["runs"])

    def test_worker_dispatches_workflow_tag_list(self):
        backend = Mock()
        backend.list_workflow_tag_runs.return_value = {"runs": [], "has_more": False}
        result = desktop_worker.dispatch(backend, {"action": "list_workflow_tag_runs", "reset": True})
        backend.list_workflow_tag_runs.assert_called_once_with(reset=True, workflow_name="build-and-push")
        self.assertEqual([], result["runs"])

    def test_worker_dispatches_tag_suggestion(self):
        backend = Mock()
        backend.suggest_tag_name.return_value = {"suggestion": "v2.5.1014-demo", "matched_count": 7}
        result = desktop_worker.dispatch(backend, {"action": "suggest_tag_name", "branch": "release-example"})
        backend.suggest_tag_name.assert_called_once_with("release-example")
        self.assertEqual("v2.5.1014-demo", result["suggestion"])

    def test_worker_dispatches_remote_tag_creation(self):
        backend = Mock()
        backend.create_remote_tag.return_value = {"created": True}
        result = desktop_worker.dispatch(backend, {
            "action": "create_remote_tag", "branch": "main",
            "tag_name": "v1.0", "confirmed": True,
        })
        self.assertEqual({"created": True}, result)
        backend.create_remote_tag.assert_called_once_with(
            "main", "v1.0", confirmed=True)

    def test_github_https_and_ssh_origins_resolve_to_one_repo_and_https_context(self):
        expected = ("example-owner_enterprise/private-repo", "https://github.com/example-owner_enterprise/private-repo.git")
        self.assertEqual(expected, desktop_backend._repository_from_remote(
            "https://github.com/example-owner_enterprise/private-repo.git"))
        self.assertEqual(expected, desktop_backend._repository_from_remote(
            "git@github.com:example-owner_enterprise/private-repo.git"))
        self.assertEqual(expected, desktop_backend._repository_from_remote(
            "ssh://git@github.com/example-owner_enterprise/private-repo.git"))

    def test_origins_with_embedded_credentials_or_non_github_hosts_are_rejected(self):
        for remote in (
            "https://user:synthetic-token@github.com/owner/repo.git",
            "https://example.invalid/owner/repo.git",
            "http://github.com/owner/repo.git",
            "ssh://user:password@github.com/owner/repo.git",
        ):
            with self.subTest(remote=remote.split("@")[0]):
                with self.assertRaises(desktop_backend.DesktopBackendError):
                    desktop_backend._repository_from_remote(remote)

    def test_credential_helper_is_called_without_repo_cwd_or_secret_arguments(self):
        token = "synthetic-token-not-for-output"
        completed = Mock(returncode=0, stdout=(
            "protocol=https\nhost=github.com\npath=owner/repo.git\n"
            "username=synthetic-user\npassword=" + token + "\n"), stderr="")
        with patch.object(desktop_backend.subprocess, "run", return_value=completed) as run:
            found = desktop_backend._credential_for_https_url(
                "https://github.com/owner/repo.git")
        self.assertEqual(token, found)
        args, kwargs = run.call_args
        self.assertEqual(["git", "credential", "fill"], args[0])
        self.assertNotIn(token, " ".join(args[0]))
        self.assertNotIn(token, kwargs["input"])
        self.assertNotEqual("/some/selected/repository", kwargs["cwd"])
        self.assertEqual("0", kwargs["env"]["GIT_TERMINAL_PROMPT"])

    def test_connect_verifies_identity_without_returning_the_token(self):
        token = "synthetic-access-token"
        with tempfile.TemporaryDirectory() as root:
            with patch.object(desktop_backend, "_git_output", side_effect=[
                    root, "https://github.com/example-owner_enterprise/private-repo.git", "feature"]), \
                    patch.object(desktop_backend, "_credential_for_https_url", return_value=token), \
                    patch.object(desktop_backend.github_api, "verify_identity",
                                 return_value="example_user_enterprise"), \
                    patch.object(desktop_backend.github_api, "_request", return_value=(
                        {"private": True, "default_branch": "main", "permissions": {"pull": True}}, {})):
                backend = desktop_backend.DesktopBackend()
                result = backend.connect_repository(root)
        self.assertEqual("example_user_enterprise", result["login"])
        self.assertEqual("example-owner_enterprise/private-repo", result["repository"])
        self.assertTrue(result["private"])
        self.assertNotIn("token", result)
        self.assertEqual(token, backend.token)

    def test_manual_token_is_verified_without_calling_git_credential_helper(self):
        token = "synthetic-token"
        with tempfile.TemporaryDirectory() as root:
            with patch.object(desktop_backend, "_git_output", side_effect=[
                    root, "https://github.com/owner/repo.git", "main"]), \
                    patch.object(desktop_backend, "_credential_for_https_url") as helper, \
                    patch.object(desktop_backend.github_api, "verify_identity", return_value="synthetic-user"), \
                    patch.object(desktop_backend.github_api, "_request", return_value=(
                        {"private": True, "default_branch": "main", "permissions": {"pull": True}}, {})):
                backend = desktop_backend.DesktopBackend()
                result = backend.connect_repository(root, token=token, expected_repository="owner/repo")
        helper.assert_not_called()
        self.assertTrue(backend.manual_token)
        self.assertNotIn(token, repr(result))

    def test_manual_token_is_not_used_if_repository_origin_changed(self):
        backend = desktop_backend.DesktopBackend()
        with patch.object(backend, "describe_repository", return_value={
                "root": "/tmp/synthetic-checkout", "repository": "owner/other"}), \
                patch.object(desktop_backend.github_api, "verify_identity") as verify:
            with self.assertRaises(desktop_backend.DesktopBackendError) as caught:
                backend.connect_repository("/tmp/synthetic-checkout", token="synthetic-token",
                                           expected_repository="owner/repo")
        self.assertEqual("repository_changed", caught.exception.code)
        verify.assert_not_called()

    def test_manual_fetch_uses_environment_not_git_arguments_or_url(self):
        backend = desktop_backend.DesktopBackend()
        backend.repository_path = "/tmp/synthetic-checkout"
        backend.token = "synthetic-token"
        backend.manual_token = True
        with patch.object(desktop_backend, "_git_run", return_value=Mock(returncode=0, stdout="")) as git:
            backend._fetch_output("fetch", "origin")
        args, kwargs = git.call_args
        self.assertNotIn(backend.token, repr(args))
        self.assertNotIn(backend.token, repr(kwargs.get("input_text")))
        self.assertEqual("false", kwargs["extra_env"]["GIT_CONFIG_VALUE_1"])
        self.assertTrue(kwargs["extra_env"]["GIT_CONFIG_VALUE_2"].startswith("Authorization: Basic "))
        self.assertNotIn(backend.token, str(backend.activity()))

    def test_create_requires_confirmation_and_current_inspection(self):
        backend = desktop_backend.DesktopBackend()
        backend.token = "synthetic-token"
        backend.login = "synthetic-user"
        backend.repository = "owner/repo"
        backend.comparison = {
            "base": "main", "source": "feature", "complete": True, "ahead_by": 1,
            "base_sha": "a" * 40, "source_sha": "b" * 40,
        }
        with patch.object(desktop_backend.github_api, "GitHubAPI") as api_factory:
            with self.assertRaises(desktop_backend.DesktopBackendError) as caught:
                backend.create("main", "feature", "Title", "Body", [], confirmed=False)
            self.assertEqual("confirmation_required", caught.exception.code)
            backend.inspection = None
            with self.assertRaises(desktop_backend.DesktopBackendError) as caught:
                backend.create("main", "feature", "Title", "Body", [], confirmed=True)
            self.assertEqual("inspection_required", caught.exception.code)
            api_factory.assert_not_called()

    def test_create_does_not_duplicate_a_pr_found_by_inspection(self):
        backend = desktop_backend.DesktopBackend()
        backend.token = "synthetic-token"
        backend.login = "synthetic-user"
        backend.repository = "owner/repo"
        backend.comparison = {
            "base": "main", "source": "feature", "complete": True, "ahead_by": 1,
            "base_sha": "a" * 40, "source_sha": "b" * 40,
        }
        existing = {"number": 17, "url": "https://github.com/owner/repo/pull/17", "title": "Existing"}
        backend.inspection = {"base": "main", "source": "feature", "exists": True,
                              "pull_request": existing}
        with patch.object(desktop_backend.github_api, "GitHubAPI") as api_factory:
            result = backend.create("main", "feature", "Title", "Body", [], confirmed=True)
        self.assertFalse(result["created"])
        self.assertEqual(17, result["existing"]["number"])
        api_factory.assert_not_called()

    def test_create_uncertain_result_invalidates_inspection_before_any_retry(self):
        backend = desktop_backend.DesktopBackend()
        backend.token = "synthetic-token"
        backend.login = "synthetic-user"
        backend.repository = "owner/repo"
        backend.comparison = {
            "base": "main", "source": "feature", "complete": True, "ahead_by": 1,
            "base_sha": "a" * 40, "source_sha": "b" * 40,
        }
        backend.inspection = {"base": "main", "source": "feature", "exists": False}
        api = Mock()
        api.create.side_effect = desktop_backend.github_api.APIError(
            "github_write_uncertain", "synthetic uncertain result", 502)
        with patch.object(desktop_backend.github_api, "GitHubAPI", return_value=api):
            with self.assertRaises(desktop_backend.DesktopBackendError) as caught:
                backend.create("main", "feature", "Title", "Body", [], confirmed=True)
        self.assertEqual("github_write_uncertain", caught.exception.code)
        self.assertIsNone(backend.inspection)
        api.create.assert_called_once()

    def test_unauthorized_api_response_clears_the_cached_token(self):
        backend = desktop_backend.DesktopBackend()
        backend.token = "synthetic-token"
        backend.login = "synthetic-user"
        backend.repository = "owner/repo"
        backend.fetched = True
        backend.branches = [{"name": "main"}, {"name": "feature"}]
        api = Mock()
        api.compare.side_effect = desktop_backend.github_api.APIError(
            "github_auth_failed", "synthetic revoked token", 401)
        with patch.object(desktop_backend.github_api, "GitHubAPI", return_value=api):
            with self.assertRaises(desktop_backend.DesktopBackendError) as caught:
                backend.compare("main", "feature")
        self.assertEqual("github_auth_failed", caught.exception.code)
        self.assertIsNone(backend.token)
        self.assertIsNone(backend.login)

    def test_saved_reviewers_reload_as_preferences_without_credentials(self):
        backend = desktop_backend.DesktopBackend()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / ".preferences.json"
            desktop_backend.preferences_store.save_preferences({
                "github_app_client_id": "Iv1.syntheticClientId",
                "repo": "keep-existing-setting",
            }, path)
            draft = {
                "local_repository_path": "/tmp/synthetic-checkout",
                "repository": "owner/repo", "base_branch": "main", "source_branch": "feature",
                "title": "Synthetic title", "body": "Synthetic body",
                "reviewers": ["reviewer_example"],
            }
            with patch.object(desktop_backend.preferences_store, "install_path", return_value=path):
                result = backend.save_settings(draft)
                loaded = backend.load_settings()
            self.assertEqual(draft, result["draft"])
            self.assertEqual(draft, loaded["draft"])
            saved = desktop_backend.preferences_store.read_preferences(path)
            self.assertEqual("Iv1.syntheticClientId", saved["github_app_client_id"])
            self.assertEqual("keep-existing-setting", saved["repo"])
            self.assertNotIn("token", saved)

    def test_auto_branch_selections_are_per_repository_and_preserve_drafts(self):
        backend = desktop_backend.DesktopBackend()
        backend.fetched = True
        backend.branches = [{"name": name} for name in ("main", "feature", "dev", "release")]
        draft = {"repository": "owner/repo-a", "base_branch": "main", "source_branch": "feature",
                 "title": "Saved title", "body": "Saved body", "reviewers": ["reviewer_example"]}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / ".preferences.json"
            desktop_backend.preferences_store.save_preferences({
                "github_app_client_id": "Iv1.syntheticClientId", "desktop_pr_workflow": draft,
            }, path)
            with patch.object(desktop_backend.preferences_store, "install_path", return_value=path):
                backend.repository = "Owner/Repo-A"
                backend.remember_branch_selection("main", "feature")
                backend.repository = "owner/repo-b"
                backend.remember_branch_selection("dev", "release")
                backend.repository = "owner/repo-a"
                backend.remember_branch_selection("dev", "feature")
                backend.save_settings(draft)
                restarted = desktop_backend.DesktopBackend()
                restarted.repository = "owner/repo-a"
                loaded_a = restarted.load_settings()
                restarted.repository = "OWNER/REPO-B"
                loaded_b = restarted.load_settings()
                stored = desktop_backend.preferences_store.read_preferences(path)
        self.assertEqual(draft, loaded_a["draft"])
        self.assertEqual(("dev", "feature"), (loaded_a["branch_selection"]["base_branch"],
                                               loaded_a["branch_selection"]["source_branch"]))
        self.assertEqual(("dev", "release"), (loaded_b["branch_selection"]["base_branch"],
                                               loaded_b["branch_selection"]["source_branch"]))
        self.assertEqual(2, len(stored["desktop_branch_selections"]))
        self.assertEqual("Iv1.syntheticClientId", stored["github_app_client_id"])
        self.assertNotIn("token", repr(stored).lower())

    def test_auto_branch_selection_saves_partial_selection_and_rejects_unverified_refs(self):
        backend = desktop_backend.DesktopBackend()
        backend.repository = "owner/repo"
        backend.fetched = True
        backend.branches = [{"name": "main"}, {"name": "feature"}]
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(desktop_backend.preferences_store, "install_path",
                             return_value=Path(temp) / ".preferences.json"):
            backend.remember_branch_selection("main", "")
            selection = backend.load_settings()["branch_selection"]
            self.assertEqual("main", selection["base_branch"])
            self.assertEqual("", selection["source_branch"])
            with self.assertRaises(desktop_backend.DesktopBackendError) as caught:
                backend.remember_branch_selection("local-only", "feature")
            self.assertEqual("branch_unverified", caught.exception.code)
            self.assertEqual(selection, backend.load_settings()["branch_selection"])

    def test_remember_local_repository_path_preserves_existing_preferences(self):
        backend = desktop_backend.DesktopBackend()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / ".preferences.json"
            desktop_backend.preferences_store.save_preferences({
                "github_app_client_id": "Iv1.syntheticClientId",
                "repo": "keep-existing-setting",
            }, path)
            with patch.object(desktop_backend.preferences_store, "install_path", return_value=path):
                result = backend.remember_local_repository_path("/tmp/synthetic-checkout")
            saved = desktop_backend.preferences_store.read_preferences(path)
        self.assertTrue(result["saved"])
        self.assertEqual("/tmp/synthetic-checkout",
                         saved["desktop_pr_workflow"]["local_repository_path"])
        self.assertEqual("Iv1.syntheticClientId", saved["github_app_client_id"])
        self.assertEqual("keep-existing-setting", saved["repo"])

    def test_fetch_updates_only_origin_tracking_refs_and_records_safe_summary(self):
        backend = desktop_backend.DesktopBackend()
        backend.repository_path = "/tmp/synthetic-checkout"
        backend.repository = "owner/repo"
        before = "origin/main\t" + "a" * 40 + "\norigin/HEAD\t" + "a" * 40
        after = ("origin/main\t" + "b" * 40 + "\n"
                 "origin/feature\t" + "c" * 40 + "\n"
                 "origin/HEAD\t" + "b" * 40)
        with patch.object(desktop_backend, "_git_output", side_effect=[before, "", after]):
            result = backend.fetch_remote()
        self.assertTrue(backend.fetched)
        self.assertFalse(backend.tags_fresh)
        self.assertEqual(2, result["count"])
        self.assertEqual(1, result["added"])
        self.assertEqual(1, result["updated"])
        self.assertEqual({"main", "feature"}, {item["name"] for item in result["branches"]})
        log = next(record for record in reversed(backend.activity()["records"])
                   if record["stage"] == "fetch")
        self.assertEqual("fetch", log["stage"])
        self.assertEqual("ok", log["outcome"])
        self.assertIn("+ origin/feature", log["detail"])
        self.assertIn("~ origin/main", log["detail"])
        self.assertNotIn("/tmp/synthetic-checkout", str(log))

    def test_fetch_activity_survives_reconnect_but_credentials_are_cleared(self):
        backend = desktop_backend.DesktopBackend()
        backend.token = "synthetic-token"
        backend._record("fetch", "ok", "Repository owner/repo: fetched origin branches.")
        backend.sign_out()
        self.assertIsNone(backend.token)
        self.assertEqual("fetch", backend.activity()["records"][0]["stage"])
        self.assertNotIn("synthetic-token", str(backend.activity()))

    def test_fetch_failure_records_error_even_if_refs_cannot_be_read(self):
        backend = desktop_backend.DesktopBackend()
        backend.repository_path = "/tmp/synthetic-checkout"
        backend.repository = "owner/repo"
        with patch.object(backend, "_remote_ref_map", side_effect=desktop_backend.DesktopBackendError(
                "git_repository_invalid", "synthetic failure")):
            with self.assertRaises(desktop_backend.DesktopBackendError):
                backend.fetch_remote()
        self.assertEqual("error", backend.activity()["records"][-1]["outcome"])
        self.assertEqual("fetch", backend.activity()["records"][-1]["stage"])

    def test_branch_fetch_does_not_fetch_remote_tags(self):
        backend = desktop_backend.DesktopBackend()
        backend.repository_path = "/tmp/synthetic-checkout"
        backend.repository = "owner/repo"
        before_branches = "origin/main\t" + "a" * 40
        after_branches = before_branches
        with patch.object(desktop_backend, "_git_output", side_effect=[
                before_branches, "", after_branches]) as git_output:
            result = backend.fetch_remote()
        self.assertFalse(result["tags_fresh"])
        self.assertEqual(3, git_output.call_count)
        self.assertFalse(any(
            "refs/tags/*" in " ".join(map(str, call.args))
            for call in git_output.call_args_list))

    def test_remote_tag_fetch_happens_on_demand_and_updates_safe_summary(self):
        backend = desktop_backend.DesktopBackend()
        backend.repository_path = "/tmp/synthetic-checkout"
        backend.repository = "owner/repo"
        backend.fetched = True
        before_tags = "refs/remotes/origin-tags/v1.0\t1700000000\t" + "b" * 40 + "\t"
        after_tags = (before_tags + "\nrefs/remotes/origin-tags/v1.1\t1700000001\t"
                      + "c" * 40 + "\t")
        with patch.object(desktop_backend, "_git_output", side_effect=[before_tags, "", after_tags]):
            result = backend.fetch_remote_tags()
        self.assertEqual(2, result["count"])
        self.assertTrue(backend.tags_fresh)
        self.assertIn("1 new", backend.activity()["records"][-1]["detail"])
        self.assertNotIn("v1.1", str(backend.activity()))

    def test_branch_tag_list_maps_remote_tags_to_reachable_branches(self):
        backend = desktop_backend.DesktopBackend()
        backend.repository_path = "/tmp/synthetic-checkout"
        backend.repository = "owner/repo"
        backend.fetched = True
        backend.tags_fresh = True
        backend.branches = [
            {"name": "main", "sha": "a" * 40},
            {"name": "feature/topic", "sha": "b" * 40},
            {"name": "release", "sha": "c" * 40},
        ]
        remote_tags = {
            "v2.0": {"name": "v2.0", "created_at": "2026-01-02", "created_epoch": 2,
                     "sha": "d" * 40},
            "v1.0": {"name": "v1.0", "created_at": "2025-01-02", "created_epoch": 1,
                     "sha": "e" * 40},
        }
        with patch.object(backend, "_tag_ref_map", return_value=remote_tags), \
                patch.object(desktop_backend, "_git_output", side_effect=[
                    "refs/remotes/origin/main\nrefs/remotes/origin/feature/topic",
                    "refs/remotes/origin/feature/topic",
                ]):
            result = backend.list_branch_tags()
        rows = {branch["name"]: branch for branch in result["branches"]}
        names = lambda branch: [result["tags"][index]["name"] for index in rows[branch]["tag_indexes"]]
        self.assertEqual(["v2.0"], names("main"))
        self.assertEqual(["v2.0", "v1.0"], names("feature/topic"))
        self.assertEqual([], rows["release"]["tag_indexes"])
        self.assertEqual(2, result["tagged_branch_count"])

    def test_branches_sort_by_their_newest_tag_time_then_name(self):
        backend = desktop_backend.DesktopBackend()
        backend.repository_path = "/tmp/synthetic-checkout"
        backend.repository = "owner/repo"
        backend.fetched = backend.tags_fresh = True
        backend.branches = [{"name": name, "sha": "a" * 40}
                            for name in ("untagged", "old", "new-z", "new-a")]
        tags = {
            "old-tag": {"name": "old-tag", "created_at": "2025-01-01", "created_epoch": 1,
                        "sha": "b" * 40},
            "new-tag": {"name": "new-tag", "created_at": "2026-01-01", "created_epoch": 2,
                        "sha": "c" * 40},
        }
        with patch.object(backend, "_tag_ref_map", return_value=tags), \
                patch.object(desktop_backend, "_git_output", side_effect=[
                    "refs/remotes/origin/new-z\nrefs/remotes/origin/new-a",
                    "refs/remotes/origin/old\nrefs/remotes/origin/new-z",
                ]):
            result = backend.list_branch_tags()
        self.assertEqual(["new-a", "new-z", "old", "untagged"],
                         [branch["name"] for branch in result["branches"]])
        row = result["branches"][1]
        self.assertEqual(["new-tag", "old-tag"],
                         [result["tags"][index]["name"] for index in row["tag_indexes"]])

    def test_large_branch_tag_response_fits_gui_ipc_limit(self):
        import json
        from desktop_client import MAX_RESPONSE_CHARS
        backend = desktop_backend.DesktopBackend()
        backend.repository_path = "/tmp/synthetic-checkout"
        backend.repository = "owner/repo"
        backend.fetched = backend.tags_fresh = True
        backend.branches = [{"name": f"branch/{i:04}", "sha": "a" * 40} for i in range(900)]
        tags = {f"release/{i:03}": {"name": f"release/{i:03}", "created_at": "2026-01-02T00:00:00+00:00",
                                    "created_epoch": i, "sha": "b" * 40} for i in range(172)}
        refs = "\n".join("refs/remotes/origin/" + branch["name"] for branch in backend.branches)
        with patch.object(backend, "_tag_ref_map", return_value=tags), \
                patch.object(desktop_backend, "_git_output", return_value=refs):
            result = backend.list_branch_tags()
        response = json.dumps({"ok": True, "data": result}, ensure_ascii=False, separators=(",", ":"))
        self.assertLess(len(response), MAX_RESPONSE_CHARS)
        self.assertEqual(900, result["tagged_branch_count"])
        self.assertEqual(172, len(result["branches"][0]["tag_indexes"]))
        self.assertEqual("release/171", result["tags"][result["branches"][0]["tag_indexes"][0]]["name"])

    def test_tag_map_preserves_lightweight_tag_at_end_of_output(self):
        backend = desktop_backend.DesktopBackend()
        backend.repository_path = "/tmp/synthetic-checkout"
        output = "refs/remotes/origin-tags/v1\t1700000000\t" + "a" * 40
        with patch.object(desktop_backend, "_git_output", return_value=output):
            tags = backend._tag_ref_map("refs/remotes/origin-tags")
        self.assertEqual("a" * 40, tags["v1"]["sha"])

    def test_branch_tag_list_fetches_tags_only_when_not_fresh(self):
        backend = desktop_backend.DesktopBackend()
        backend.repository_path = "/tmp/synthetic-checkout"
        backend.repository = "owner/repo"
        backend.fetched = True
        backend.branches = [{"name": "main", "sha": "a" * 40}]

        def mark_fresh():
            backend.tags_fresh = True

        with patch.object(backend, "fetch_remote_tags", side_effect=mark_fresh) as fetch_tags, \
                patch.object(backend, "_tag_ref_map", return_value={}), \
                patch.object(desktop_backend, "_git_output", return_value=""):
            result = backend.list_branch_tags()
        fetch_tags.assert_called_once_with()
        self.assertEqual([], result["branches"][0]["tag_indexes"])

    def test_origin_tag_targets_resolves_annotated_tags_without_fetch(self):
        backend = desktop_backend.DesktopBackend()
        backend.repository_path = "/tmp/synthetic-checkout"
        output = ("a" * 40 + "\trefs/tags/lightweight\n"
                  + "b" * 40 + "\trefs/tags/annotated\n"
                  + "c" * 40 + "\trefs/tags/annotated^{}\n")
        with patch.object(backend, "_fetch_output", return_value=output) as fetch:
            result = backend._origin_tag_targets()
        self.assertEqual({"lightweight": "a" * 40, "annotated": "c" * 40}, result)
        fetch.assert_called_once_with("ls-remote", "--tags", "origin", timeout=60)

    def test_workflow_tag_list_groups_latest_runs_and_loads_more(self):
        backend = desktop_backend.DesktopBackend()
        backend.repository = "owner/repo"
        backend.token = "synthetic-token"
        backend.login = "synthetic-user"
        def run(index, tag=None, sha=None):
            return {"id": index + 1, "run_number": index + 1, "run_attempt": 1,
                    "ref": tag or f"v{index}", "sha": sha or f"{index:040x}",
                    "status": "completed", "conclusion": "success", "created_at": "2026-01-01T00:00:00Z"}
        tags = {f"v{i}": f"{i:040x}" for i in range(22)}
        api = Mock()
        api.workflow_runs.side_effect = [
            {"runs": [run(i) for i in range(21)] + [run(30, "v0", tags["v0"])],
             "next_page": 2, "scan_limited": False},
            {"runs": [run(21), run(40, "v20", "f" * 40)], "next_page": None, "scan_limited": False},
        ]
        api.workflow_run_jobs.return_value = [{"environment": "cluster/d1", "state": "success"}]
        branch_result = {"available": True, "exact": ["main"], "related": [],
                         "exact_count": 1, "related_count": 0}
        with patch.object(backend, "_api", return_value=api), \
                patch.object(backend, "_origin_tag_targets", return_value=tags) as read_tags, \
                patch.object(backend, "_workflow_branch_associations", return_value=branch_result):
            first = backend.list_workflow_tag_runs(reset=True)
            second = backend.list_workflow_tag_runs()
        self.assertEqual(20, len(first["runs"]))
        self.assertTrue(first["has_more"])
        self.assertEqual(["v20", "v21"], [row["tag"] for row in second["runs"]])
        self.assertFalse(second["has_more"])
        self.assertEqual(22, api.workflow_run_jobs.call_count)
        read_tags.assert_called_once()
        self.assertEqual("cluster/d1", first["runs"][0]["jobs"][0]["environment"])

    def test_workflow_job_reads_run_in_bounded_parallel_workers(self):
        import threading
        backend = desktop_backend.DesktopBackend()
        backend.repository = "owner/repo"
        backend.token = "synthetic-token"
        backend.login = "synthetic-user"
        runs = [{"id": i + 1, "run_number": i + 1, "ref": f"v{i}", "sha": f"{i:040x}"}
                for i in range(5)]
        api = Mock()
        api.workflow_runs.return_value = {"runs": runs, "next_page": None, "scan_limited": False}
        barrier = threading.Barrier(5)
        lock = threading.Lock()
        counters = {"active": 0, "peak": 0}
        def load_jobs(_repo, _run_id):
            with lock:
                counters["active"] += 1
                counters["peak"] = max(counters["peak"], counters["active"])
            barrier.wait(timeout=5)
            with lock:
                counters["active"] -= 1
            return []
        api.workflow_run_jobs.side_effect = load_jobs
        tags = {f"v{i}": f"{i:040x}" for i in range(5)}
        with patch.object(backend, "_api", return_value=api), \
                patch.object(backend, "_origin_tag_targets", return_value=tags), \
                patch.object(backend, "_workflow_branch_associations", return_value={"available": False}):
            result = backend.list_workflow_tag_runs(reset=True)
        self.assertEqual(5, counters["peak"])
        self.assertEqual(5, len(result["runs"]))
        self.assertTrue(all(not row["jobs_error"] for row in result["runs"]))

    def test_workflow_auth_failure_clears_session_and_does_not_commit_page(self):
        backend = desktop_backend.DesktopBackend()
        backend.repository = "owner/repo"
        backend.token = "synthetic-token"
        backend.login = "synthetic-user"
        api = Mock()
        api.workflow_runs.return_value = {"runs": [{"id": 1, "ref": "v1", "sha": "a" * 40}],
                                          "next_page": None, "scan_limited": False}
        api.workflow_run_jobs.side_effect = desktop_backend.github_api.APIError("github_auth_failed", "revoked", 401)
        with patch.object(backend, "_api", return_value=api), \
                patch.object(backend, "_origin_tag_targets", return_value={"v1": "a" * 40}), \
                patch.object(backend, "_workflow_branch_associations", return_value={"available": False}):
            with self.assertRaises(desktop_backend.DesktopBackendError) as caught:
                backend.list_workflow_tag_runs(reset=True)
        self.assertEqual("github_auth_failed", caught.exception.code)
        self.assertIsNone(backend.token)
        self.assertIsNone(backend.workflow_state)

    def test_workflow_tag_jobs_failure_is_reported_not_shown_as_success(self):
        backend = desktop_backend.DesktopBackend()
        backend.repository = "owner/repo"
        backend.token = "synthetic-token"
        backend.login = "synthetic-user"
        api = Mock()
        api.workflow_runs.return_value = {"runs": [{"id": 1, "run_number": 1, "run_attempt": 1,
            "ref": "v1", "sha": "a" * 40, "status": "completed", "conclusion": "success"}],
            "next_page": None, "scan_limited": False}
        api.workflow_run_jobs.side_effect = desktop_backend.github_api.APIError("github_access_denied", "denied", 403)
        with patch.object(backend, "_api", return_value=api), \
                patch.object(backend, "_origin_tag_targets", return_value={"v1": "a" * 40}), \
                patch.object(backend, "_workflow_branch_associations", return_value={"available": False}):
            result = backend.list_workflow_tag_runs(reset=True)
        self.assertEqual([], result["runs"][0]["jobs"])
        self.assertTrue(result["runs"][0]["jobs_error"])
        self.assertFalse(result["runs"][0]["branches"]["available"])

    def test_workflow_branch_associations_distinguish_tip_and_reachable(self):
        backend = desktop_backend.DesktopBackend()
        backend.repository_path = "/tmp/synthetic-checkout"
        backend.fetched = True
        backend.branches = [{"name": "main", "sha": "a" * 40}, {"name": "release-example", "sha": "b" * 40}]
        with patch.object(desktop_backend, "_git_output", return_value=(
                "refs/remotes/origin/main\nrefs/remotes/origin/release-example\nrefs/remotes/origin/HEAD")):
            result = backend._workflow_branch_associations("a" * 40)
        self.assertEqual(["main"], result["exact"])
        self.assertEqual(["release-example"], result["related"])
        self.assertEqual(1, result["related_count"])

    def test_configured_tag_suggestion_uses_highest_numeric_origin_version(self):
        backend = desktop_backend.DesktopBackend()
        backend.repository_path = "/tmp/synthetic-checkout"
        backend.repository = "owner/repo"
        backend.fetched = True
        backend.branches = [{"name": "release-example", "sha": "a" * 40},
                            {"name": "other", "sha": "b" * 40}]
        output = "\n".join("a" * 40 + "\trefs/tags/" + name for name in (
            "v2.5.9-demo", "v2.5.1012-demo", "v2.5.1013-demo", "v1.1.99-demo",
            "v2.5.100-other", "v2.5.1014-demo-extra"))
        with patch.object(backend, "load_settings", return_value={"tag_rules": SYNTHETIC_TAG_RULES}), \
                patch.object(backend, "_fetch_output", return_value=output) as fetch:
            result = backend.suggest_tag_name("release-example")
        self.assertEqual("v2.5.1014-demo", result["suggestion"])
        self.assertEqual(3, result["matched_count"])
        fetch.assert_called_once_with("ls-remote", "--tags", "--refs", "origin", timeout=60)

    def test_tag_suggestion_does_not_apply_to_other_branch_or_invent_initial_version(self):
        backend = desktop_backend.DesktopBackend()
        backend.repository_path = "/tmp/synthetic-checkout"
        backend.repository = "owner/repo"
        backend.fetched = True
        backend.branches = [{"name": "release-example", "sha": "a" * 40},
                            {"name": "other", "sha": "b" * 40}]
        with patch.object(backend, "load_settings", return_value={"tag_rules": SYNTHETIC_TAG_RULES}), \
                patch.object(backend, "_fetch_output", return_value="") as fetch:
            self.assertEqual("", backend.suggest_tag_name("other")["suggestion"])
            fetch.assert_not_called()
            self.assertEqual("", backend.suggest_tag_name("release-example")["suggestion"])
            fetch.assert_called_once()

    def test_suggestion_uses_local_rules_and_treats_prefix_as_literal(self):
        backend = desktop_backend.DesktopBackend()
        backend.repository_path = "/tmp/synthetic-checkout"
        backend.repository = "example-owner/example-repo"
        backend.fetched = True
        backend.branches = [{"name": "release-example", "sha": "a" * 40}]
        rules = {"branch_filter": "release-*", "suggestions": [
            {"branch": "release-example", "prefix": "release.v+", "suffix": "-demo"},
        ]}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / ".preferences.json"
            desktop_backend.preferences_store.save_preferences({"desktop_tag_rules": rules}, path)
            output = "a" * 40 + "\trefs/tags/release.v+9-demo\n" + "b" * 40 + "\trefs/tags/releaseXv99-demo"
            with patch.object(desktop_backend.preferences_store, "install_path", return_value=path), \
                    patch.object(backend, "_fetch_output", return_value=output):
                result = backend.suggest_tag_name("release-example")
        self.assertEqual("release.v+10-demo", result["suggestion"])
        self.assertEqual(1, result["matched_count"])

    def test_tag_suggestion_rejects_unverified_branch_and_sanitizes_lookup_failure(self):
        backend = desktop_backend.DesktopBackend()
        backend.repository_path = "/tmp/synthetic-checkout"
        backend.repository = "owner/repo"
        with self.assertRaises(desktop_backend.DesktopBackendError) as caught:
            backend.suggest_tag_name("release-example")
        self.assertEqual("branch_unverified", caught.exception.code)
        backend.fetched = True
        backend.branches = [{"name": "release-example", "sha": "a" * 40}]
        with patch.object(backend, "load_settings", return_value={"tag_rules": SYNTHETIC_TAG_RULES}), \
                patch.object(backend, "_fetch_output", side_effect=desktop_backend.DesktopBackendError(
                    "git_fetch_failed", "synthetic secret diagnostic")):
            with self.assertRaises(desktop_backend.DesktopBackendError) as caught:
                backend.suggest_tag_name("release-example")
        self.assertEqual("tag_suggestion_unavailable", caught.exception.code)
        self.assertNotIn("synthetic secret diagnostic", caught.exception.message)

    def test_create_remote_tag_uses_selected_origin_tip_then_invalidates_tag_cache(self):
        backend = desktop_backend.DesktopBackend()
        backend.repository_path = "/tmp/synthetic-checkout"
        backend.repository = "owner/repo"
        backend.token = "synthetic-token"
        backend.login = "synthetic-user"
        backend.fetched = True
        backend.branches = [{"name": "main", "sha": "a" * 40}]
        api = Mock()
        api.branch_tip.return_value = {"name": "main", "sha": "a" * 40}
        api.create_tag.return_value = {"created": True, "tag": "v1.0", "sha": "a" * 40}
        backend.tags_fresh = True
        with patch.object(backend, "_api", return_value=api):
            result = backend.create_remote_tag("main", "v1.0", confirmed=True)
        self.assertTrue(result["created"])
        self.assertFalse(backend.tags_fresh)
        api.create_tag.assert_called_once_with("owner/repo", "v1.0", "a" * 40)
        api.branch_tip.assert_called_once_with("owner/repo", "main")

    def test_create_remote_tag_refuses_existing_name_without_refresh(self):
        backend = desktop_backend.DesktopBackend()
        backend.repository_path = "/tmp/synthetic-checkout"
        backend.repository = "owner/repo"
        backend.token = "synthetic-token"
        backend.login = "synthetic-user"
        backend.fetched = True
        backend.branches = [{"name": "main", "sha": "a" * 40}]
        api = Mock()
        api.branch_tip.return_value = {"name": "main", "sha": "a" * 40}
        api.create_tag.side_effect = desktop_backend.github_api.APIError(
            "tag_exists", "A tag with that name already exists on GitHub.", 409)
        with patch.object(backend, "_api", return_value=api):
            with self.assertRaises(desktop_backend.DesktopBackendError) as caught:
                backend.create_remote_tag("main", "v1.0", confirmed=True)
        self.assertEqual("tag_exists", caught.exception.code)
        self.assertIn("already exists", caught.exception.message)
        api.create_tag.assert_called_once()

    def test_source_commit_log_uses_author_time_and_sorts_newest_first(self):
        backend = desktop_backend.DesktopBackend()
        backend.repository_path = "/tmp/synthetic-checkout"
        output = ("a" * 40 + "\x002025-01-02T03:04:05+00:00\x00Author One\x00Feature one\x00"
                  + "b" * 40 + "\x002026-01-02T03:04:05+00:00\x00Author Two\x00Feature two\x00")
        with patch.object(desktop_backend, "_git_output", return_value=output) as git_output:
            commits, truncated = backend._source_commit_log("feature/topic")
        self.assertFalse(truncated)
        self.assertEqual(["b" * 40, "a" * 40], [item["sha"] for item in commits])
        self.assertEqual("Author Two", commits[0]["author"])
        self.assertEqual("2026-01-02T03:04:05+00:00", commits[0]["authored_at"])
        self.assertEqual("Feature two", commits[0]["subject"])
        args = git_output.call_args.args
        self.assertIn("refs/remotes/origin/feature/topic", args)
        self.assertIn("--no-show-signature", args)
        self.assertIn("-z", args)

    def test_source_commit_log_caps_history_at_100(self):
        backend = desktop_backend.DesktopBackend()
        backend.repository_path = "/tmp/synthetic-checkout"
        output = "".join(f"{i:040x}\x002026-01-02T03:04:05+00:00\x00Author\x00Title {i}\x00"
                         for i in range(101))
        with patch.object(desktop_backend, "_git_output", return_value=output):
            commits, truncated = backend._source_commit_log("main")
        self.assertEqual(100, len(commits))
        self.assertTrue(truncated)

    def test_source_commit_detail_requires_current_commit_and_redacts_secrets(self):
        backend = desktop_backend.DesktopBackend()
        backend.repository_path = "/tmp/synthetic-checkout"
        backend.fetched = True
        backend.comparison = {"source_commits": [{"sha": "a" * 40}]}
        with patch.object(desktop_backend, "_git_output", return_value="Subject\n\nBody token=synthetic-secret") as git:
            result = backend.source_commit_detail("a" * 40)
        self.assertIn("Subject", result["message"])
        self.assertNotIn("synthetic-secret", result["message"])
        self.assertFalse(result["truncated"])
        self.assertIn("a" * 40, git.call_args.args)
        with self.assertRaises(desktop_backend.DesktopBackendError) as caught:
            backend.source_commit_detail("b" * 40)
        self.assertEqual("commit_unverified", caught.exception.code)

    def test_source_commit_detail_reports_truncation(self):
        backend = desktop_backend.DesktopBackend()
        backend.repository_path = "/tmp/synthetic-checkout"
        backend.fetched = True
        backend.comparison = {"source_commits": [{"sha": "a" * 40}]}
        with patch.object(desktop_backend, "_git_output", return_value="x" * 100_001):
            result = backend.source_commit_detail("a" * 40)
        self.assertTrue(result["truncated"])
        self.assertEqual(100_000, len(result["message"]))

    def test_copilot_is_offered_as_a_special_search_result_not_a_graphql_user(self):
        backend = desktop_backend.DesktopBackend()
        api = Mock()
        api.reviewers.return_value = {"reviewers": [], "next_cursor": None, "complete": True}
        with patch.object(desktop_backend.github_api, "GitHubAPI", return_value=api):
            backend.token = "synthetic-token"
            backend.login = "synthetic-user"
            backend.repository = "owner/repo"
            result = backend.search_reviewers("Copilot")
        self.assertEqual("copilot", result["reviewers"][0]["login"])
        self.assertEqual("copilot", result["reviewers"][0]["kind"])
        self.assertTrue(backend.copilot_reviewer_available)
        api.reviewers.assert_not_called()

    def test_selected_copilot_reviewer_uses_dedicated_review_request(self):
        backend = desktop_backend.DesktopBackend()
        backend.token = "synthetic-token"
        backend.login = "synthetic-user"
        backend.repository = "owner/repo"
        backend.comparison = {
            "base": "main", "source": "feature", "complete": True, "ahead_by": 1,
            "base_sha": "a" * 40, "source_sha": "b" * 40,
        }
        backend.inspection = {"base": "main", "source": "feature", "exists": False}
        copilot_alias = "copilot"
        backend.reviewer_candidates.add(copilot_alias)
        backend.copilot_reviewer_available = True
        api = Mock()
        api.create.return_value = {"created": True, "pull_request": {"number": 19}}
        api.request_copilot_review.return_value = {"requested": True, "confirmed": True}
        with patch.object(desktop_backend.github_api, "GitHubAPI", return_value=api):
            result = backend.create("main", "feature", "Title", "Body", [copilot_alias], confirmed=True)
        self.assertTrue(result["created"])
        self.assertTrue(result["copilot_review"]["confirmed"])
        self.assertEqual([], api.create.call_args.args[5])
        api.request_copilot_review.assert_called_once_with("owner/repo", 19)

    def test_preferences_reject_auth_material(self):
        backend = desktop_backend.DesktopBackend()
        with self.assertRaises(desktop_backend.DesktopBackendError) as caught:
            backend.save_settings({"repository": "owner/repo", "token": "synthetic-token"})
        self.assertEqual("preferences_invalid", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
