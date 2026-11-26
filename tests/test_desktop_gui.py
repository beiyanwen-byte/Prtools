"""Tk GUI smoke tests; skipped on Python builds without tkinter."""

import unittest

try:
    import tkinter  # noqa: F401
except ModuleNotFoundError:
    tkinter_available = False
else:
    tkinter_available = True

if tkinter_available:
    from desktop_gui import DesktopApp


SYNTHETIC_TAG_RULES = {"branch_filter": "release-*", "suggestions": [
    {"branch": "release-example", "prefix": "v2.5.", "suffix": "-demo"},
]}


@unittest.skipUnless(tkinter_available, "tkinter is unavailable in this Python environment")
class DesktopGuiBranchSearchTests(unittest.TestCase):
    class StubBackend:
        def __init__(self, draft=None):
            self.draft = draft or {}
            self.compare_calls = []
            self.inspect_calls = []
            self.branch_save_calls = []
            self.commit_detail_calls = []
            self.tag_suggestion_calls = []
            self.workflow_calls = []
            self.tag_load_calls = 0
            self.comparison = {
                "repository": "owner/repo", "base": "main", "source": "feature",
                "base_sha": "a" * 40, "source_sha": "b" * 40,
                "status": "ahead", "ahead_by": 1, "behind_by": 0,
                "complete": True, "files": [], "commits": [],
                "default_title": "Synthetic title", "default_body": "Synthetic body",
            }

        def sign_out(self):
            pass

        def close(self):
            pass

        def load_settings(self):
            return {"draft": self.draft, "tag_rules": SYNTHETIC_TAG_RULES}

        def remember_branch_selection(self, base, source):
            self.branch_save_calls.append((base, source))
            return {"saved": True}

        def compare(self, base, source):
            self.compare_calls.append((base, source))
            return {**self.comparison, "base": base, "source": source}

        def inspect(self, base, source):
            self.inspect_calls.append((base, source))
            return {"exists": False, "pull_request": None}

        def list_branch_tags(self):
            self.tag_load_calls += 1
            return {"branch_count": 0, "tagged_branch_count": 0, "branches": []}

        def list_workflow_tag_runs(self, *, reset=False, workflow_name="build-and-push"):

            self.workflow_calls.append((reset, workflow_name))
            return {"runs": [], "has_more": False, "scan_limited": False, "warning": ""}

        def source_commit_detail(self, sha):
            self.commit_detail_calls.append(sha)
            return {"sha": sha, "message": "Synthetic title\n\nFull message body", "truncated": False}

        def suggest_tag_name(self, branch):
            self.tag_suggestion_calls.append(branch)
            return {"suggestion": "v2.5.1014-demo", "matched_count": 7}

        def activity(self):
            return {"records": []}

    def test_base_and_source_search_filter_remote_branches_case_insensitively(self):
        app = DesktopApp(self.StubBackend())
        app.withdraw()
        try:
            app.remote_branches = ["main", "release-example", "feature/example_developing", "feature/payment"]
            app._filter_branch_options()
            self.assertEqual("4/4", app.base_search_count.get())

            app.base_search.set("RELEASE-EXAMPLE")
            self.assertEqual(("origin/release-example",), tuple(app.base_box["values"]))
            self.assertEqual("1/4", app.base_search_count.get())
            self.assertEqual(("origin/main", "origin/release-example", "origin/feature/example_developing", "origin/feature/payment"),
                             tuple(app.source_box["values"]))

            app.source_search.set("EXAMPLE_DEVELOPING")
            self.assertEqual(("origin/feature/example_developing",), tuple(app.source_box["values"]))
            self.assertEqual("1/4", app.source_search_count.get())

            app.base_search.set("")
            self.assertEqual(4, len(app.base_box["values"]))
        finally:
            app.destroy()

    def test_api_branch_value_is_unprefixed_only_after_remote_selection(self):
        class Variable:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

        self.assertEqual("release-example", DesktopApp._selected_remote_branch(Variable("origin/release-example")))
        self.assertEqual("", DesktopApp._selected_remote_branch(Variable("feature/local-only")))

    def test_saved_reviewers_are_prefilled_unverified_until_current_search_matches(self):
        app = DesktopApp(self.StubBackend())
        app.withdraw()
        try:
            app._connected(({
                "repository": "owner/repo", "login": "synthetic-user", "private": True,
                "default_branch": "main", "current_branch": "feature",
            }, [{"name": "main", "sha": "a" * 40},
                {"name": "feature", "sha": "b" * 40}], {
                "repository": "owner/repo", "base_branch": "main", "source_branch": "feature",
                "title": "Saved title", "body": "Saved body", "reviewers": ["REVIEWER_EXAMPLE"],
            }, {"count": 2, "added": 0, "updated": 0}, "", None, SYNTHETIC_TAG_RULES))
            self.assertEqual(["REVIEWER_EXAMPLE"], app.selected_reviewers)
            self.assertEqual(set(), app.verified_reviewers)
            self.assertTrue(app.create_button.instate(["disabled"]))

            app._reviewers_found({"reviewers": [{"login": "reviewer_example", "name": "Reviewer"}]})
            self.assertEqual(["reviewer_example"], app.selected_reviewers)
            self.assertEqual({"reviewer_example"}, app.verified_reviewers)
        finally:
            app.destroy()

    def test_remembered_repository_branches_restore_without_reusing_unrelated_draft(self):
        from unittest.mock import patch
        app = DesktopApp(self.StubBackend())
        app.withdraw()
        try:
            info = {"repository": "owner/repo", "login": "synthetic-user", "private": True,
                    "current_branch": "feature"}
            branches = [{"name": name, "sha": "a" * 40} for name in ("main", "feature", "dev")]
            draft = {"repository": "owner/repo", "base_branch": "main", "source_branch": "feature",
                     "title": "Old title", "body": "Old body", "reviewers": ["old-reviewer"]}
            selection = {"repository": "OWNER/REPO", "base_branch": "dev", "source_branch": "feature"}
            with patch.object(app, "_schedule_auto_compare") as compare:
                app._connected((info, branches, draft, {"tags_fresh": False}, "", selection, SYNTHETIC_TAG_RULES))
            self.assertEqual("origin/dev", app.base.get())
            self.assertEqual("origin/feature", app.source.get())
            self.assertEqual("", app.title_var.get())
            self.assertEqual([], app.selected_reviewers)
            compare.assert_called_once()
        finally:
            app.destroy()

    def test_missing_remembered_branch_is_left_blank_not_replaced(self):
        from unittest.mock import patch
        app = DesktopApp(self.StubBackend())
        app.withdraw()
        try:
            info = {"repository": "owner/repo", "login": "synthetic-user", "private": True,
                    "current_branch": "feature"}
            branches = [{"name": "feature", "sha": "a" * 40}]
            selection = {"repository": "owner/repo", "base_branch": "removed", "source_branch": "feature"}
            with patch.object(app, "_schedule_auto_compare") as compare:
                app._connected((info, branches, {}, {"tags_fresh": False}, "", selection, SYNTHETIC_TAG_RULES))
            self.assertEqual("", app.base.get())
            self.assertEqual("origin/feature", app.source.get())
            self.assertIn("saved branch is unavailable", app.status.get())
            compare.assert_not_called()
        finally:
            app.destroy()

    def test_branch_changes_are_saved_automatically_per_selection(self):
        from unittest.mock import patch
        backend = self.StubBackend()
        app = DesktopApp(backend)
        app.withdraw()
        try:
            app.repo_info = {"repository": "owner/repo"}
            app.remote_fresh = True
            app.remote_branches = ["main", "feature"]
            with patch.object(app, "_schedule_auto_compare"):
                app.base.set("origin/main")
                app._branch_selection_changed()
                deadline = __import__("time").monotonic() + 5
                while app.busy and __import__("time").monotonic() < deadline:
                    app.update()
                    __import__("time").sleep(.02)
                app.source.set("origin/feature")
                app._branch_selection_changed()
                deadline = __import__("time").monotonic() + 5
                while app.busy and __import__("time").monotonic() < deadline:
                    app.update()
                    __import__("time").sleep(.02)
            self.assertEqual([("main", ""), ("main", "feature")], backend.branch_save_calls)
        finally:
            app.destroy()

    def test_branch_save_failure_warns_and_does_not_block_comparison(self):
        from unittest.mock import patch
        from desktop_backend import DesktopBackendError
        backend = self.StubBackend()
        def fail(_base, _source):
            raise DesktopBackendError("preferences_save_failed", "Could not save preferences.")
        backend.remember_branch_selection = fail
        app = DesktopApp(backend)
        app.withdraw()
        try:
            app.workspace_path.set("synthetic-workspace")
            app.repo_info = {"repository": "owner/repo"}
            app.remote_fresh = True
            app.base.set("origin/main")
            app.source.set("origin/feature")
            with patch("desktop_gui.messagebox.showwarning") as warning, \
                    patch.object(app, "_schedule_auto_compare") as compare:
                app._branch_selection_changed()
                deadline = __import__("time").monotonic() + 5
                while app.busy and __import__("time").monotonic() < deadline:
                    app.update()
                    __import__("time").sleep(.02)
                warning.assert_called_once()
                compare.assert_called_once()
        finally:
            app.destroy()

    def test_copilot_appears_as_special_reviewer_and_can_be_selected(self):
        app = DesktopApp(self.StubBackend())
        app.withdraw()
        try:
            bot = "copilot"
            app._reviewers_found({"reviewers": [{
                "login": bot, "name": "GitHub Copilot code review (special reviewer)",
                "kind": "copilot",
            }]})
            self.assertIn("Copilot code review", app.reviewer_results.get(0))
            app.reviewer_results.selection_set(0)
            app.add_reviewer()
            self.assertEqual([bot], app.selected_reviewers)
            self.assertIn(bot, app.special_reviewers)
            self.assertIn(bot, app.verified_reviewers)
        finally:
            app.destroy()

    def test_last_workspace_directory_is_prefilled_on_startup(self):
        expected = r"C:\synthetic\checkout"
        app = DesktopApp(self.StubBackend({"local_repository_path": expected}))
        app.withdraw()
        try:
            deadline = __import__("time").monotonic() + 3
            while not app.workspace_path.get() and __import__("time").monotonic() < deadline:
                app.update()
                __import__("time").sleep(.02)
            self.assertEqual(expected, app.workspace_path.get())
            self.assertEqual("", app.repo_path.get())
        finally:
            app.destroy()

    def test_workspace_scans_only_root_and_direct_git_children(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "direct" / ".git").mkdir(parents=True)
            (root / "worktree").mkdir()
            (root / "worktree" / ".git").write_text("gitdir: ../other", encoding="utf-8")
            (root / "nested" / "too-deep" / ".git").mkdir(parents=True)
            repositories = DesktopApp._find_workspace_repositories(directory)
        self.assertEqual(["direct", "worktree"], [name for _path, name in repositories])

    def test_saved_windows_token_is_used_after_git_helper_fails(self):
        from unittest.mock import patch
        from desktop_backend import DesktopBackendError
        backend = self.StubBackend()
        backend.describe_repository = lambda _path: {"repository": "owner/repo"}
        calls = []
        def connect(_path, **kwargs):
            calls.append(kwargs)
            if not kwargs:
                raise DesktopBackendError("credential_missing", "missing")
            return {"repository": "owner/repo", "login": "synthetic-user", "private": True}
        backend.connect_repository = connect
        backend.fetch_remote = lambda: {"count": 0, "added": 0, "updated": 0, "tags_fresh": False}
        backend.load_branches = lambda: []
        app = DesktopApp(backend)
        app.withdraw()
        try:
            app.repo_path.set("synthetic-workspace/project")
            with patch("desktop_gui.repository_credentials.read", return_value="synthetic-token") as read, \
                    patch("desktop_gui.repository_credentials.save") as save:
                app.connect()
                deadline = __import__("time").monotonic() + 5
                while app.busy and __import__("time").monotonic() < deadline:
                    app.update()
                    __import__("time").sleep(.02)
                read.assert_called_once_with("owner/repo")
                save.assert_not_called()
            self.assertEqual([{}, {"token": "synthetic-token", "expected_repository": "owner/repo"}], calls)
        finally:
            app.destroy()

    def test_explicit_saved_token_remains_selected_when_git_helper_succeeds(self):
        from unittest.mock import patch
        backend = self.StubBackend()
        calls = []
        def connect(_path, **kwargs):
            calls.append(kwargs)
            return {"repository": "owner/repo", "login": "synthetic-user", "private": True}
        backend.connect_repository = connect
        backend.fetch_remote = lambda: {"count": 0, "added": 0, "updated": 0, "tags_fresh": False}
        backend.load_branches = lambda: []
        app = DesktopApp(backend)
        app.withdraw()
        try:
            app.repo_path.set("synthetic-workspace/project")
            with patch("desktop_gui.repository_credentials.read", return_value="synthetic-token"):
                app.connect()
                deadline = __import__("time").monotonic() + 5
                while app.busy and __import__("time").monotonic() < deadline:
                    app.update()
                    __import__("time").sleep(.02)
            self.assertEqual([{}, {"token": "synthetic-token", "expected_repository": "owner/repo"}], calls)
        finally:
            app.destroy()

    def test_manual_token_is_saved_only_after_repository_verification(self):
        from unittest.mock import patch
        backend = self.StubBackend()
        backend.describe_repository = lambda _path: {"repository": "owner/repo"}
        backend.connect_repository = lambda _path, **_kwargs: {
            "repository": "owner/repo", "login": "synthetic-user", "private": True}
        backend.fetch_remote = lambda: {"count": 0, "added": 0, "updated": 0, "tags_fresh": False}
        backend.load_branches = lambda: []
        app = DesktopApp(backend)
        app.withdraw()
        try:
            app.repo_path.set("synthetic-workspace/project")
            with patch("desktop_gui.repository_credentials.save") as save:
                app.connect(token="synthetic-token")
                deadline = __import__("time").monotonic() + 5
                while app.busy and __import__("time").monotonic() < deadline:
                    app.update()
                    __import__("time").sleep(.02)
                save.assert_called_once_with("owner/repo", "synthetic-token")
            self.assertEqual("owner/repo", app.repo_info["repository"])
        finally:
            app.destroy()

    def test_invalid_manual_token_is_never_saved(self):
        from unittest.mock import patch
        from desktop_backend import DesktopBackendError
        backend = self.StubBackend()
        backend.describe_repository = lambda _path: {"repository": "owner/repo"}
        def reject(_path, **_kwargs):
            raise DesktopBackendError("github_auth_failed", "bad token")
        backend.connect_repository = reject
        app = DesktopApp(backend)
        app.withdraw()
        try:
            app.repo_path.set("synthetic-workspace/project")
            with patch("desktop_gui.repository_credentials.save") as save, \
                    patch("desktop_gui.simpledialog.askstring", return_value=None):
                app.connect(token="invalid-synthetic-token")
                deadline = __import__("time").monotonic() + 5
                while app.busy and __import__("time").monotonic() < deadline:
                    app.update()
                    __import__("time").sleep(.02)
                save.assert_not_called()
        finally:
            app.destroy()

    def test_auth_failure_prompts_for_token_but_network_failure_does_not(self):
        from unittest.mock import patch
        from desktop_backend import DesktopBackendError
        app = DesktopApp(self.StubBackend())
        app.withdraw()
        try:
            app.repo_path.set("synthetic-workspace/project")
            with patch("desktop_gui.simpledialog.askstring", return_value=None) as prompt:
                app._connect_failed(DesktopBackendError("github_unavailable", "network unavailable"))
                prompt.assert_not_called()
                app._connect_failed(DesktopBackendError("credential_missing", "missing"))
                prompt.assert_called_once()
        finally:
            app.destroy()

    def test_selecting_workspace_repository_connects_automatically(self):
        from unittest.mock import patch
        app = DesktopApp(self.StubBackend())
        app.withdraw()
        try:
            app.workspace_path.set("synthetic-workspace")
            app._workspace_loaded([("synthetic-workspace/project", "project")])
            root = app.repo_tree.get_children()[0]
            row = app.repo_tree.get_children(root)[0]
            app.repo_tree.selection_set(row)
            with patch.object(app, "connect") as connect:
                app._repository_selected()
            self.assertEqual("synthetic-workspace/project", app.repo_path.get())
            connect.assert_called_once_with()
        finally:
            app.destroy()

    def test_selecting_both_branches_auto_compares_then_inspects(self):
        backend = self.StubBackend()
        app = DesktopApp(backend)
        app.withdraw()
        try:
            startup_deadline = __import__("time").monotonic() + 3
            while app.busy and __import__("time").monotonic() < startup_deadline:
                app.update()
                __import__("time").sleep(.02)
            app.repo_info = {"repository": "owner/repo"}
            app.remote_fresh = True
            app.remote_branches = ["main", "feature"]
            app._filter_branch_options()
            app.base.set("origin/main")
            app.source.set("origin/feature")
            app._branch_selection_changed()

            deadline = __import__("time").monotonic() + 5
            while (app.busy or not backend.compare_calls or not backend.inspect_calls) \
                    and __import__("time").monotonic() < deadline:
                app.update()
                __import__("time").sleep(.02)
            self.assertEqual([("main", "feature")], backend.compare_calls)
            self.assertEqual([("main", "feature")], backend.inspect_calls)
            self.assertIsNotNone(app.inspection)
        finally:
            app.destroy()

    def test_changed_files_are_grouped_in_expanded_directories_with_totals(self):
        backend = self.StubBackend()
        app = DesktopApp(backend)
        app.withdraw()
        try:
            app.repo_info = {"repository": "owner/repo"}
            files = [
                {"filename": "src/main/java/Order.py", "additions": 4, "deletions": 2,
                 "patch_status": "available", "patch": "@@ first\n+order"},
                {"filename": "README.md", "additions": 1, "deletions": 1,
                 "patch_status": "available", "patch": "@@ readme\n+readme"},
                {"filename": "src/test/OrderTest.py", "additions": 2, "deletions": 0,
                 "patch_status": "available", "patch": "@@ test\n+test"},
                {"filename": "src/main/java/Util.py", "additions": 1, "deletions": 3,
                 "patch_status": "available", "patch": "@@ util\n+util"},
                {"filename": "src/main/resources/data.json", "additions": None, "deletions": 5,
                 "patch_status": "unavailable"},
            ]
            app._compared({**backend.comparison, "complete": False, "files": files})
            def child(parent, name):
                return next(iid for iid in app.files.get_children(parent)
                            if app.files.item(iid, "text") == name)
            roots = app.files.get_children()
            self.assertEqual(["src/", "README.md"], [app.files.item(iid, "text") for iid in roots])
            src = child("", "src/")
            main = child(src, "main/")
            java = child(main, "java/")
            order = child(java, "Order.py")
            self.assertTrue(app.files.item(src, "open"))
            self.assertTrue(app.files.item(main, "open"))
            self.assertTrue(app.files.item(java, "open"))
            self.assertEqual(("4", "?", "10"), tuple(map(str, app.files.item(src, "values"))))
            self.assertEqual(("3", "?", "10"), tuple(map(str, app.files.item(main, "values"))))
            self.assertEqual(("2", "5", "5"), tuple(map(str, app.files.item(java, "values"))))
            app.files.selection_set(order)
            app.show_file()
            self.assertIn("+order", app.patch.get("1.0", "end-1c"))
            app.files.selection_set(java)
            app.show_file()
            self.assertIn("Select a file", app.patch.get("1.0", "end-1c"))
            app.files.selection_set(child("", "README.md"))
            app.show_file()
            self.assertIn("+readme", app.patch.get("1.0", "end-1c"))
        finally:
            app.destroy()

    def test_same_filename_in_different_directories_opens_correct_patch(self):
        backend = self.StubBackend()
        app = DesktopApp(backend)
        app.withdraw()
        try:
            app.file_items = [
                {"filename": "alpha/config.yaml", "additions": 1, "deletions": 0,
                 "patch_status": "available", "patch": "+alpha"},
                {"filename": "beta/config.yaml", "additions": 0, "deletions": 1,
                 "patch_status": "available", "patch": "-beta"},
            ]
            app._render_file_tree()
            beta = next(iid for iid in app.files.get_children() if app.files.item(iid, "text") == "beta/")
            file_row = app.files.get_children(beta)[0]
            app.files.selection_set(file_row)
            app.show_file()
            self.assertEqual("-beta", app.patch.get("1.0", "end-1c"))
        finally:
            app.destroy()

    def test_source_commit_list_shows_author_time_and_loads_message_on_selection(self):
        backend = self.StubBackend()
        app = DesktopApp(backend)
        app.withdraw()
        try:
            app.repo_info = {"repository": "owner/repo"}
            app._compared({**backend.comparison, "complete": False, "source_commits": [
                {"sha": "a" * 40, "authored_at": "2026-01-02T03:04:05+00:00",
                 "author": "Synthetic Author", "subject": "Synthetic title"},
            ]})
            rows = app.commit_list.get_children()
            self.assertEqual(1, len(rows))
            self.assertEqual(("2026-01-02T03:04:05+00:00", "Synthetic Author", "Synthetic title"),
                             app.commit_list.item(rows[0], "values"))
            self.assertEqual([], backend.commit_detail_calls)
            app.commit_list.selection_set(rows[0])
            app._commit_selected()
            deadline = __import__("time").monotonic() + 5
            while app.busy and __import__("time").monotonic() < deadline:
                app.update()
                __import__("time").sleep(.02)
            self.assertEqual(["a" * 40], backend.commit_detail_calls)
            self.assertIn("Full message body", app.commit_view.get("1.0", "end-1c"))
        finally:
            app.destroy()

    def test_fetch_activity_displays_changed_branches(self):
        app = DesktopApp(self.StubBackend())
        app.withdraw()
        try:
            app._render_activity({"records": [{"time": "10:00:00", "stage": "fetch", "outcome": "ok",
                                                "detail": "Fetched 2 branches.\n  + origin/feature\n  ~ origin/main"}]})
            text = app.activity_view.get("1.0", "end-1c")
            self.assertIn("fetch", text)
            self.assertIn("+ origin/feature", text)
            self.assertIn("~ origin/main", text)
        finally:
            app.destroy()

    def test_workflow_page_accepts_build_and_push_and_related_repository_name(self):
        app = DesktopApp(self.StubBackend())
        app.withdraw()
        try:
            app.repo_info = {"repository": "example-owner/example-model"}
            app.workflow_name.set("build-and-push")
            app._workflow_tags_loaded({"runs": [{
                "id": 123, "tag": "v1.0.0-test-d1", "status": "completed",
                "conclusion": "success", "jobs": [], "branches": {
                    "available": True, "exact": ["main"], "exact_count": 1,
                    "related": [], "related_count": 0,
                }}], "has_more": False}, reset=True)
            self.assertIn("build-and-push", app.workflow_result.cget("text") or "")
            self.assertEqual("v1.0.0-test-d1", app.workflow_list.item("0", "text"))
        finally:
            app.destroy()

    def test_workflow_page_accepts_build_and_push_or_build_and_deploy_name(self):
        app = DesktopApp(self.StubBackend())
        app.withdraw()
        try:
            app.repo_info = {"repository": "owner/repo"}
            app.workflow_name.set("build-and-push")
            app.load_workflow_tags(reset=True)
            deadline = __import__("time").monotonic() + 5
            while app.busy and __import__("time").monotonic() < deadline:
                app.update()
                __import__("time").sleep(.02)
            self.assertTrue(app.workflow_loaded)
            app.workflow_name.set("build-and-deploy.yaml")
            app.load_workflow_tags(reset=True)
            deadline = __import__("time").monotonic() + 5
            while app.busy and __import__("time").monotonic() < deadline:
                app.update()
                __import__("time").sleep(.02)
            self.assertTrue(app.workflow_loaded)
        finally:
            app.destroy()

    def test_workflow_page_displays_run_environments_and_safe_link(self):
        from unittest.mock import patch
        app = DesktopApp(self.StubBackend())
        app.withdraw()
        try:
            app.repo_info = {"repository": "owner/repo"}
            row = {"id": 123, "tag": "v2.5.1013-demo", "run_number": 27, "run_attempt": 1,
                   "status": "waiting", "conclusion": "", "created_at": "2026-01-02T03:04:05Z",
                   "actor": "synthetic-user", "jobs_error": "", "jobs": [
                       {"environment": "cluster/d1", "state": "success", "sync_confirmed": True},
                       {"environment": "cluster/d2", "state": "waiting"},
                       {"environment": "cluster/t1", "state": "failure"},
                   ], "branches": {"available": True, "exact": ["main"], "exact_count": 1,
                                   "related": ["release-example"], "related_count": 1},
                   "url": "https://evil.invalid/"}
            app.workflow_name.set("build-and-push")
            app._workflow_tags_loaded({"runs": [row], "has_more": True, "scan_limited": False,
                                       "warning": ""}, reset=True)
            ids = app.workflow_list.get_children()
            self.assertEqual(1, len(ids))
            self.assertEqual("v2.5.1013-demo", app.workflow_list.item(ids[0], "text"))
            values = app.workflow_list.item(ids[0], "values")
            self.assertIn("cluster/d1", values[1])
            self.assertIn("✓", values[1])
            self.assertIn("cluster/d2", values[2])
            self.assertIn("cluster/t1", values[3])
            self.assertIn("has_failures", app.workflow_list.item(ids[0], "tags"))
            self.assertIn("头匹配", values[4])
            self.assertIn("origin/main", values[4])
            self.assertIn("状态不会自动更新", app.workflow_result.cget("text"))
            detail = app.workflow_detail.get("1.0", "end-1c")
            self.assertIn("origin/main", detail)
            self.assertNotIn("origin/release-example", detail)
            self.assertIn("Job 成功不保证", detail)
            app.workflow_show_related.set(True)
            app._workflow_selected()
            self.assertIn("origin/release-example", app.workflow_detail.get("1.0", "end-1c"))
            with patch("desktop_gui.webbrowser.open") as open_url:
                app._open_workflow_run()
            open_url.assert_called_once_with("https://github.com/owner/repo/actions/runs/123")
            self.assertTrue(app.workflow_more_button.instate(["!disabled"]))
        finally:
            app.destroy()

    def test_workflow_shows_associated_branch_names_when_no_tip_matches(self):
        app = DesktopApp(self.StubBackend())
        app.withdraw()
        try:
            app.repo_info = {"repository": "owner/repo"}
            row = {"id": 123, "tag": "v1", "status": "completed", "jobs": [],
                   "branches": {"available": True, "exact": [], "exact_count": 0,
                                "related": ["release-example", "feature/payment"], "related_count": 2}}
            app._workflow_tags_loaded({"runs": [row], "has_more": False}, reset=True)
            branch_cell = app.workflow_list.item("0", "values")[4]
            self.assertIn("关联 2", branch_cell)
            self.assertIn("origin/release-example", branch_cell)
            self.assertIn("origin/feature/payment", branch_cell)
            detail = app.workflow_detail.get("1.0", "end-1c")
            self.assertIn("origin/release-example", detail)
            self.assertIn("origin/feature/payment", detail)
        finally:
            app.destroy()

    def test_failed_environments_precede_cancelled_ones_in_summary(self):
        app = DesktopApp(self.StubBackend())
        app.withdraw()
        try:
            app.repo_info = {"repository": "owner/repo"}
            jobs = [{"environment": f"example-cluster/d{i}", "state": "cancelled"} for i in range(7)]
            jobs.append({"environment": "example-cluster/t1", "state": "failure"})
            app._workflow_tags_loaded({"runs": [{"id": 1, "tag": "v1", "status": "waiting",
                "jobs": jobs, "branches": {"available": False}}], "has_more": False}, reset=True)
            failure_cell = app.workflow_list.item("0", "values")[3]
            self.assertTrue(failure_cell.startswith("example-cluster/t1"))
            detail = app.workflow_detail.get("1.0", "end-1c")
            self.assertIn("example-cluster/t1 (failure)", detail)
            self.assertIn("example-cluster/d6 (cancelled)", detail)
        finally:
            app.destroy()

    def test_workflow_refresh_displays_failed_t1_even_when_run_waits(self):
        app = DesktopApp(self.StubBackend())
        app.withdraw()
        try:
            app.repo_info = {"repository": "owner/repo"}
            row = {"id": 123, "tag": "v2.5.1014-demo", "run_number": 29,
                   "run_attempt": 1, "status": "waiting", "conclusion": "",
                   "jobs": [{"environment": "example-cluster/t1", "state": "waiting"}],
                   "branches": {"available": False}}
            app._workflow_tags_loaded({"runs": [row], "has_more": False}, reset=True)
            self.assertEqual("—", app.workflow_list.item("0", "values")[3])
            failed = {**row, "jobs": [{"environment": "example-cluster/t1", "state": "failure"}]}
            app._workflow_tags_loaded({"runs": [failed], "has_more": False}, reset=True)
            values = app.workflow_list.item("0", "values")
            self.assertEqual("waiting", values[0])
            self.assertEqual("example-cluster/t1", values[3])
            self.assertIn("has_failures", app.workflow_list.item("0", "tags"))
            self.assertIn(app.workflow_refreshed_at, app.workflow_result.cget("text"))
        finally:
            app.destroy()

    def test_workflow_page_only_loads_on_open_and_supports_manual_refresh(self):
        backend = self.StubBackend()
        app = DesktopApp(backend)
        app.withdraw()
        try:
            app.workspace_path.set("synthetic-workspace")
            app.repo_info = {"repository": "owner/repo"}
            app.workflow_name.set("build-and-push")
            self.assertEqual([], backend.workflow_calls)
            app.notebook.select(app.workflow_tab)
            app._notebook_tab_changed()
            deadline = __import__("time").monotonic() + 5
            while app.busy and __import__("time").monotonic() < deadline:
                app.update()
                __import__("time").sleep(.02)
            self.assertEqual([(True, "build-and-push")], backend.workflow_calls)
            app._notebook_tab_changed()
            self.assertEqual([(True, "build-and-push")], backend.workflow_calls)
            app.load_workflow_tags(reset=True)
            deadline = __import__("time").monotonic() + 5
            while app.busy and __import__("time").monotonic() < deadline:
                app.update()
                __import__("time").sleep(.02)
            self.assertEqual([(True, "build-and-push"), (True, "build-and-push")], backend.workflow_calls)
        finally:
            app.destroy()

    def test_workflow_page_marks_unavailable_jobs_and_branches(self):
        app = DesktopApp(self.StubBackend())
        app.withdraw()
        try:
            app.repo_info = {"repository": "owner/repo"}
            app._workflow_tags_loaded({"runs": [{"id": 1, "tag": "v1", "status": "completed",
                "jobs": [], "jobs_error": "unavailable", "branches": {"available": False}}],
                "has_more": False}, reset=True)
            values = app.workflow_list.item("0", "values")
            self.assertEqual("读取失败", values[1])
            self.assertIn("unavailable", values[4])
            self.assertIn("部署环境未能读取", app.workflow_detail.get("1.0", "end-1c"))
        finally:
            app.destroy()

    def test_tag_tab_defaults_to_tagged_branches_and_search_includes_untagged(self):
        app = DesktopApp(self.StubBackend())
        app.withdraw()
        try:
            app._branch_tags_loaded({
                "branch_count": 3, "tagged_branch_count": 2,
                "tags": [{"name": "v2.0", "sha": "c" * 40},
                         {"name": "prod-2026", "sha": "e" * 40}],
                "branches": [
                    {"name": "main", "sha": "a" * 40, "tag_indexes": [0]},
                    {"name": "feature/no-tags", "sha": "b" * 40, "tag_indexes": []},
                    {"name": "release", "sha": "e" * 40, "tag_indexes": [1]},
                ],
            })
            app.tag_branch_search.set("")
            rows = list(app.tag_rows.values())
            self.assertEqual(2, len(rows))
            self.assertIn("main", [row["branch"]["name"] for row in rows])
            self.assertNotIn("feature/no-tags", [row["branch"]["name"] for row in rows])

            app.tag_branch_search.set("NO-TAGS")
            rows = list(app.tag_rows.values())
            self.assertEqual(1, len(rows))
            self.assertEqual("feature/no-tags", rows[0]["branch"]["name"])
            self.assertIn("no tags", rows[0]["tags"].lower())

            app.tag_branch_search.set("")
            app.tag_search.set("PROD")
            rows = list(app.tag_rows.values())
            self.assertEqual(1, len(rows))
            self.assertEqual("release", rows[0]["branch"]["name"])
            self.assertEqual("prod-2026", rows[0]["tags"])
        finally:
            app.destroy()

    def test_default_branch_filter_latest_five_and_old_tag_search(self):
        app = DesktopApp(self.StubBackend())
        app.withdraw()
        try:
            app._apply_tag_rules(SYNTHETIC_TAG_RULES)
            self.assertEqual("release-*", app.tag_branch_search.get())
            tags = [{"name": f"v2.5.{i}-demo", "sha": "a" * 40} for i in range(7, 0, -1)]
            app._branch_tags_loaded({"branch_count": 3, "tagged_branch_count": 2,
                "tags": tags,
                "branches": [
                    {"name": "release-example", "sha": "a" * 40, "tag_indexes": list(range(7))},
                    {"name": "release-other", "sha": "b" * 40, "tag_indexes": []},
                    {"name": "release", "sha": "c" * 40, "tag_indexes": list(range(7))},
                ]})
            rows = list(app.tag_rows.values())
            self.assertEqual(["release-example"], [row["branch"]["name"] for row in rows])
            self.assertEqual(", ".join(tag["name"] for tag in tags[:5]), rows[0]["tags"])
            self.assertNotIn(tags[-1]["name"], rows[0]["tags"])
            app.tag_search.set("V2.5.1-DEMO")
            rows = list(app.tag_rows.values())
            self.assertEqual(["release-example"], [row["branch"]["name"] for row in rows])
            self.assertEqual("v2.5.1-demo", rows[0]["tags"])
            app.tag_search.set("")
            app.tag_branch_search.set("RELEASE-*")
            self.assertEqual(["release-example", "release-other"],
                             [row["branch"]["name"] for row in app.tag_rows.values()])
            app.tag_branch_search.set("")
            app.tag_search.set("v2.5.1-demo")
            self.assertEqual(["release-example"], [row["branch"]["name"] for row in app.tag_rows.values()])
            app.tag_show_related.set(True)
            app._render_branch_tags()
            self.assertEqual(["release-example", "release"],
                             [row["branch"]["name"] for row in app.tag_rows.values()])
        finally:
            app.destroy()

    def test_tag_search_shows_tip_first_and_hides_other_reachable_branches(self):
        app = DesktopApp(self.StubBackend())
        app.withdraw()
        try:
            app._apply_tag_rules(SYNTHETIC_TAG_RULES)
            self.assertEqual("release-*", app.tag_branch_search.get())
            app._branch_tags_loaded({"branch_count": 3, "tagged_branch_count": 3,
                "tags": [{"name": "v2.5.1012-demo", "sha": "a" * 40}],
                "branches": [
                    {"name": "release-other", "sha": "b" * 40, "tag_indexes": [0]},
                    {"name": "main", "sha": "a" * 40, "tag_indexes": [0]},
                    {"name": "other", "sha": "c" * 40, "tag_indexes": [0]},
                ]})
            self.assertEqual(["release-other"], [row["branch"]["name"] for row in app.tag_rows.values()])
            app.tag_search.set("v2.5.1012-demo")
            rows = list(app.tag_rows.values())
            self.assertEqual(["main"], [row["branch"]["name"] for row in rows])
            self.assertIn("分支头匹配", rows[0]["relation"])
            self.assertIn("其他关联分支 2", app.tag_result.cget("text"))
            app.tag_show_related.set(True)
            app._render_branch_tags()
            rows = list(app.tag_rows.values())
            self.assertEqual(["main", "release-other", "other"], [row["branch"]["name"] for row in rows])
            self.assertIn("关联分支", rows[1]["relation"])
            app.tag_branch_search.set("release-*")
            self.assertEqual(["release-other"], [row["branch"]["name"] for row in app.tag_rows.values()])
            app.tag_search.set("")
            self.assertFalse(app.tag_show_related.get())
        finally:
            app.destroy()

    def test_tag_search_keeps_older_tip_matching_tag_visible(self):
        app = DesktopApp(self.StubBackend())
        app.withdraw()
        try:
            tags = [{"name": f"release-{i}", "sha": "b" * 40} for i in range(6)]
            tags.append({"name": "release-tip", "sha": "a" * 40})
            app._branch_tags_loaded({"branch_count": 1, "tagged_branch_count": 1,
                "tags": tags,
                "branches": [{"name": "main", "sha": "a" * 40,
                              "tag_indexes": list(range(len(tags)))}]})
            app.tag_search.set("release")
            row = app.tag_rows["0"]
            self.assertIn("分支头匹配", row["relation"])
            self.assertIn("release-tip", row["tags"])
            self.assertEqual(5, len(row["tags"].split(", ")))
        finally:
            app.destroy()

    def test_tag_search_without_tip_match_offers_reachable_branches(self):
        app = DesktopApp(self.StubBackend())
        app.withdraw()
        try:
            app._branch_tags_loaded({"branch_count": 1, "tagged_branch_count": 1,
                "tags": [{"name": "v1-old", "sha": "a" * 40}],
                "branches": [{"name": "release-example", "sha": "b" * 40, "tag_indexes": [0]}]})
            app.tag_search.set("v1-old")
            self.assertEqual(0, len(app.tag_rows))
            self.assertIn("分支头匹配 0 个", app.tag_result.cget("text"))
            self.assertIn("其他关联分支 1 个", app.tag_result.cget("text"))
            app.tag_show_related.set(True)
            app._render_branch_tags()
            self.assertEqual(["release-example"], [row["branch"]["name"] for row in app.tag_rows.values()])
        finally:
            app.destroy()

    @staticmethod
    def _tag_dialog_controls(app):
        from tkinter import ttk
        dialog = next(child for child in app.winfo_children() if isinstance(child, tkinter.Toplevel))
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        widgets = list(descendants(dialog))
        entry = next(widget for widget in widgets if isinstance(widget, ttk.Entry))
        button = next(widget for widget in widgets if isinstance(widget, ttk.Button)
                      and "确认创建远端 Tag" in widget.cget("text"))
        return dialog, entry, button

    def test_configured_branch_tag_dialog_prefills_live_suggestion_without_creating(self):
        from unittest.mock import Mock
        backend = self.StubBackend()
        backend.create_remote_tag = Mock()
        app = DesktopApp(backend)
        app.withdraw()
        try:
            app.repo_info = {"repository": "owner/repo"}
            app.remote_fresh = app.tags_fresh = True
            app.remote_branch_shas = {"release-example": "a" * 40}
            app._apply_tag_rules(SYNTHETIC_TAG_RULES)
            app.confirm_create_tag({"name": "release-example", "sha": "a" * 40})
            dialog, entry, button = self._tag_dialog_controls(app)
            deadline = __import__("time").monotonic() + 5
            while app.busy and __import__("time").monotonic() < deadline:
                app.update()
                __import__("time").sleep(.02)
            self.assertEqual("v2.5.1014-demo", entry.get())
            self.assertEqual(["release-example"], backend.tag_suggestion_calls)
            self.assertTrue(button.instate(["!disabled"]))
            backend.create_remote_tag.assert_not_called()
            dialog.destroy()
        finally:
            app.destroy()

    def test_suggestion_does_not_replace_name_typed_while_loading(self):
        import threading
        backend = self.StubBackend()
        released = threading.Event()
        def slow_suggestion(branch):
            released.wait(5)
            return {"suggestion": "v2.5.1014-demo", "matched_count": 7}
        backend.suggest_tag_name = slow_suggestion
        app = DesktopApp(backend)
        app.withdraw()
        try:
            app.repo_info = {"repository": "owner/repo"}
            app.remote_fresh = app.tags_fresh = True
            app.remote_branch_shas = {"release-example": "a" * 40}
            app._apply_tag_rules(SYNTHETIC_TAG_RULES)
            app.confirm_create_tag({"name": "release-example", "sha": "a" * 40})
            dialog, entry, button = self._tag_dialog_controls(app)
            entry.insert(0, "v2.5.20-demo")
            self.assertTrue(button.instate(["disabled"]))
            released.set()
            deadline = __import__("time").monotonic() + 5
            while app.busy and __import__("time").monotonic() < deadline:
                app.update()
                __import__("time").sleep(.02)
            self.assertEqual("v2.5.20-demo", entry.get())
            self.assertTrue(button.instate(["!disabled"]))
            dialog.destroy()
        finally:
            released.set()
            app.destroy()

    def test_closing_dialog_during_suggestion_does_not_create_tag(self):
        import threading
        from unittest.mock import Mock
        backend = self.StubBackend()
        released = threading.Event()
        def slow_suggestion(_branch):
            released.wait(5)
            return {"suggestion": "v2.5.1014-demo", "matched_count": 7}
        backend.suggest_tag_name = slow_suggestion
        backend.create_remote_tag = Mock()
        app = DesktopApp(backend)
        app.withdraw()
        try:
            app.repo_info = {"repository": "owner/repo"}
            app.remote_fresh = app.tags_fresh = True
            app.remote_branch_shas = {"release-example": "a" * 40}
            app._apply_tag_rules(SYNTHETIC_TAG_RULES)
            app.confirm_create_tag({"name": "release-example", "sha": "a" * 40})
            dialog, _entry, _button = self._tag_dialog_controls(app)
            dialog.destroy()
            released.set()
            deadline = __import__("time").monotonic() + 5
            while app.busy and __import__("time").monotonic() < deadline:
                app.update()
                __import__("time").sleep(.02)
            self.assertFalse(app.busy)
            backend.create_remote_tag.assert_not_called()
        finally:
            released.set()
            app.destroy()

    def test_failed_suggestion_allows_manual_name(self):
        from desktop_backend import DesktopBackendError
        backend = self.StubBackend()
        def fail(_branch):
            raise DesktopBackendError("tag_suggestion_unavailable", "Could not check origin tags.")
        backend.suggest_tag_name = fail
        app = DesktopApp(backend)
        app.withdraw()
        try:
            app.workspace_path.set("synthetic-workspace")
            app.repo_info = {"repository": "owner/repo"}
            app.remote_fresh = app.tags_fresh = True
            app.remote_branch_shas = {"release-example": "a" * 40}
            app._apply_tag_rules(SYNTHETIC_TAG_RULES)
            app.confirm_create_tag({"name": "release-example", "sha": "a" * 40})
            dialog, entry, button = self._tag_dialog_controls(app)
            deadline = __import__("time").monotonic() + 5
            while app.busy and __import__("time").monotonic() < deadline:
                app.update()
                __import__("time").sleep(.02)
            self.assertIn("Could not check origin tags", app.status.get())
            entry.insert(0, "v2.5.20-demo")
            self.assertTrue(button.instate(["!disabled"]))
            dialog.destroy()
        finally:
            app.destroy()

    def test_unconfigured_branch_does_not_request_suggestion(self):
        backend = self.StubBackend()
        app = DesktopApp(backend)
        app.withdraw()
        try:
            app.repo_info = {"repository": "owner/repo"}
            app.remote_fresh = app.tags_fresh = True
            app.remote_branch_shas = {"release-example": "a" * 40}
            self.assertEqual("", app.tag_branch_search.get())
            app.confirm_create_tag({"name": "release-example", "sha": "a" * 40})
            dialog, entry, _button = self._tag_dialog_controls(app)
            self.assertEqual("", entry.get())
            self.assertEqual([], backend.tag_suggestion_calls)
            dialog.destroy()
        finally:
            app.destroy()

    def test_other_branch_tag_dialog_does_not_request_suggestion(self):
        backend = self.StubBackend()
        app = DesktopApp(backend)
        app.withdraw()
        try:
            app.repo_info = {"repository": "owner/repo"}
            app.remote_fresh = app.tags_fresh = True
            app.remote_branch_shas = {"main": "a" * 40}
            app.confirm_create_tag({"name": "main", "sha": "a" * 40})
            dialog, entry, _button = self._tag_dialog_controls(app)
            self.assertEqual("", entry.get())
            self.assertEqual([], backend.tag_suggestion_calls)
            dialog.destroy()
        finally:
            app.destroy()

    def test_tag_text_wraps_and_create_action_still_targets_branch(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        app = DesktopApp(self.StubBackend())
        app.withdraw()
        try:
            app.repo_info = {"repository": "owner/repo"}
            app.remote_fresh = True
            app.tag_branch_search.set("")
            tags = [{"name": f"release-version-{i:02}"} for i in range(20)]
            app._branch_tags_loaded({"branch_count": 1, "tagged_branch_count": 1,
                                     "tags": tags,
                                     "branches": [{"name": "main", "sha": "a" * 40,
                                                   "tag_indexes": list(range(len(tags)))}]})
            row = app.tag_rows["0"]
            bounds = app.tag_list.bbox(row["tag_item"])
            self.assertGreater(bounds[3] - bounds[1], 40)
            self.assertGreater(row["bottom"] - row["top"], 40)
            with patch.object(app.tag_list, "winfo_width", return_value=650):
                app._render_branch_tags()
                narrow_height = app.tag_rows["0"]["bottom"] - app.tag_rows["0"]["top"]
            with patch.object(app.tag_list, "winfo_width", return_value=1100):
                app._render_branch_tags()
                wide_height = app.tag_rows["0"]["bottom"] - app.tag_rows["0"]["top"]
            self.assertGreater(narrow_height, wide_height)
            row = app.tag_rows["0"]
            with patch.object(app, "confirm_create_tag") as confirm:
                app._tag_list_click(SimpleNamespace(x=row["action_x"] + 2, y=row["top"] + 10))
            self.assertEqual("main", confirm.call_args.args[0]["name"])
        finally:
            app.destroy()

    def test_remote_tags_are_loaded_only_when_the_tag_tab_is_opened(self):
        backend = self.StubBackend()
        app = DesktopApp(backend)
        app.withdraw()
        try:
            app.repo_info = {"repository": "owner/repo"}
            app.remote_fresh = True
            self.assertEqual(0, backend.tag_load_calls)

            app.notebook.select(app.tags_tab)
            app._notebook_tab_changed()
            deadline = __import__("time").monotonic() + 5
            while app.busy and __import__("time").monotonic() < deadline:
                app.update()
                __import__("time").sleep(.02)
            app.update()

            self.assertEqual(1, backend.tag_load_calls)
            self.assertTrue(app.tags_fresh)
        finally:
            app.destroy()


if __name__ == "__main__":
    unittest.main()
