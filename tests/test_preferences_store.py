"""Synthetic, local-only checks for strict preference persistence."""

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import preferences_store as store


class PreferencesStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / ".preferences.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def write_raw(self, text):
        self.path.write_bytes(text)

    def test_preferences_remain_at_project_root_after_app_reorganization(self):
        module = self.path.parent / "app" / "desktop_backend.py"
        self.assertEqual(self.path, store.install_path(module))

    def test_strict_json_rejects_non_object_duplicates_invalid_utf8_and_constants(self):
        cases = (
            (b"[]", "preferences_invalid"),
            (b'{"repo":"one","repo":"two"}', "preferences_duplicate_key"),
            (b'{"repo":NaN}', "preferences_malformed"),
            (b'{"repo":"\xff"}', "preferences_malformed"),
        )
        for raw, code in cases:
            with self.subTest(raw=raw):
                self.write_raw(raw)
                with self.assertRaises(store.PreferencesError) as error:
                    store.read_preferences(self.path)
                self.assertEqual(code, error.exception.code)

    def test_known_fields_and_secret_fields_are_validated(self):
        for document, code in (
            ({"repo": 42}, "preferences_invalid"),
            ({"github_app_client_id": " "}, "github_app_client_id_invalid"),
            ({"desktop_pr_workflow": {"reviewers": "not-a-list"}}, "preferences_invalid"),
            ({"desktop_pr_workflow": None}, "preferences_invalid"),
            ({"nested": {"device_code": "do-not-store"}}, "preferences_secret_field"),
        ):
            with self.subTest(document=document):
                self.write_raw(json.dumps(document).encode("utf-8"))
                with self.assertRaises(store.PreferencesError) as error:
                    store.read_preferences(self.path)
                self.assertEqual(code, error.exception.code)

        self.path.unlink()
        with self.assertRaises(store.PreferencesError) as error:
            store.save_preferences({"github_token": "do-not-store"}, self.path)
        self.assertEqual("preferences_secret_field", error.exception.code)
        self.assertFalse(self.path.exists())

    def test_local_tag_rules_are_validated_and_only_seeded_if_missing(self):
        rules = {"branch_filter": "release-*", "suggestions": [
            {"branch": "release-example", "prefix": "v2.5.", "suffix": "-demo"},
        ]}
        store.save_preferences({"repo": "example-repo", "desktop_tag_rules": rules},
                               self.path, only_if_missing=True)
        store.save_preferences({"desktop_tag_rules": {"branch_filter": "other-*", "suggestions": []}},
                               self.path, only_if_missing=True)
        saved = store.read_preferences(self.path)
        self.assertEqual("example-repo", saved["repo"])
        self.assertEqual(rules, saved["desktop_tag_rules"])
        with self.assertRaises(store.PreferencesError):
            store.save_preferences({"desktop_tag_rules": {"suggestions": [
                {"branch": "release-example", "prefix": "bad\n", "suffix": "-demo"},
            ]}}, self.path)
        self.assertEqual(rules, store.read_preferences(self.path)["desktop_tag_rules"])

    def test_branch_selection_updates_merge_atomically_per_repository(self):
        errors = []
        barrier = threading.Barrier(8)
        def save(index):
            try:
                barrier.wait(timeout=5)
                store.save_preferences({"desktop_branch_selections": [{
                    "repository": f"owner/repo-{index}", "base_branch": "main", "source_branch": "feature",
                }]}, self.path)
            except Exception as error:
                errors.append(error)
        workers = [threading.Thread(target=save, args=(index,)) for index in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)
        self.assertFalse(errors)
        self.assertFalse(any(worker.is_alive() for worker in workers))
        selections = store.read_preferences(self.path)["desktop_branch_selections"]
        self.assertEqual({f"owner/repo-{index}" for index in range(8)},
                         {item["repository"] for item in selections})

    def test_branch_selection_schema_rejects_malformed_or_duplicate_repositories(self):
        for selections in (
            "not-a-list", [{"repository": "owner/repo", "base_branch": "main"}],
            [{"repository": "owner/repo", "base_branch": "main", "source_branch": "feature"},
             {"repository": "OWNER/REPO", "base_branch": "dev", "source_branch": "feature"}],
        ):
            with self.subTest(selections=selections), self.assertRaises(store.PreferencesError):
                store.save_preferences({"desktop_branch_selections": selections}, self.path)
        self.assertFalse(self.path.exists())

    def test_client_id_syntax_is_checked_on_read_and_before_authentication(self):
        valid = ("synthetic-public-id", "Alphanumeric123", "Iv1.Abc123")
        for value in valid:
            with self.subTest(valid=value):
                self.write_raw(json.dumps({"github_app_client_id": value}).encode())
                self.assertEqual(value, store.get_client_id(self.path))
        invalid = (
            "", " ", " padded", "padded ", "two words", "a\tb",
            "https://example.invalid/client", "a/b", r"a\b", "a:b", "a?b",
            "a&b", "a=b", "a,b", "a;b", "a.b", "Iv1.", "-abc", "abc-",
            "abc--def", "a_b", "non-ascii-é", "x" * 257,
        )
        for value in invalid:
            with self.subTest(invalid=value):
                self.write_raw(json.dumps({"github_app_client_id": value}).encode())
                with self.assertRaises(store.PreferencesError) as error:
                    store.read_preferences(self.path)
                self.assertEqual("github_app_client_id_invalid", error.exception.code)
    def test_credential_aliases_are_rejected_without_replacing_existing_file(self):
        sentinel = "synthetic-credential-value-unique"
        aliases = (
            "pat", "github_pat", "GITHUB-PAT", "githubpat", "GitHubPAT",
            "personal_access_token", "accessToken", "refresh_token",
            "accesstokens", "refresh_tokens", "github.p-a-t",
            "githubaccesstoken", "oauthRefreshToken", "secret", "client_secret",
            "api_key", "githubApiKey", "githubapikey", "access-key",
            "accesskey", "secret_key", "private_key", "password",
            "device_code", "user_code", "authorization",
        )
        for alias in aliases:
            with self.subTest(alias=alias):
                original = json.dumps({
                    "repo": "before", "nested": [{"inner": {alias: sentinel}}],
                }).encode("utf-8")
                self.write_raw(original)
                for action in (
                    lambda: store.read_preferences(self.path),
                    lambda: store.save_preferences({"base": "main"}, self.path),
                    lambda: store.save_preferences({"base": "main"}, self.path, merge=False),
                ):
                    with self.assertRaises(store.PreferencesError) as error:
                        action()
                    self.assertEqual("preferences_secret_field", error.exception.code)
                    self.assertNotIn(sentinel, str(error.exception))
                    self.assertEqual(original, self.path.read_bytes())

                clean = b'{"repo":"before"}'
                self.write_raw(clean)
                with self.assertRaises(store.PreferencesError) as error:
                    store.save_preferences(
                        {"desktop_pr_workflow": {"extra": [{alias: sentinel}]}},
                        self.path,
                    )
                self.assertEqual("preferences_secret_field", error.exception.code)
                self.assertNotIn(sentinel, str(error.exception))
                self.assertEqual(clean, self.path.read_bytes())

    def test_benign_similar_names_are_preserved(self):
        original = {
            "github_app_client_id": "public-client-id",
            "unknown": {
                "pattern": "x", "path": "y", "compatible": True,
                "secretary": "z", "tokenization": "words",
                "keyboard": "standard", "monkey": "animal",
            },
            "desktop_pr_workflow": {"title": "Original"},
        }
        self.write_raw(json.dumps(original).encode("utf-8"))
        self.assertEqual(original, store.read_preferences(self.path))
        store.save_preferences({"base": "main"}, self.path)
        saved = store.read_preferences(self.path)
        self.assertEqual(original["unknown"], saved["unknown"])
        self.assertEqual(original["github_app_client_id"], saved["github_app_client_id"])
        self.assertEqual(original["desktop_pr_workflow"], saved["desktop_pr_workflow"])
        self.assertEqual("main", saved["base"])

    def test_merge_preserves_unknown_settings_client_id_and_nested_workflow_fields(self):
        original = {
            "github_app_client_id": "public-client-id",
            "existing_setting": {"enabled": True},
            "desktop_pr_workflow": {"repository": "owner/repo", "title": "Original"},
        }
        self.path.write_text(json.dumps(original), encoding="utf-8")

        store.save_preferences(
            {"desktop_pr_workflow": {"title": "Updated", "reviewers": ["reviewer"]}},
            self.path,
        )

        saved = store.read_preferences(self.path)
        self.assertEqual("public-client-id", store.get_client_id(self.path))
        self.assertEqual(original["existing_setting"], saved["existing_setting"])
        self.assertEqual(
            {"repository": "owner/repo", "title": "Updated", "reviewers": ["reviewer"]},
            saved["desktop_pr_workflow"],
        )

    def test_missing_client_id_does_not_fall_back_to_environment_or_legacy_file(self):
        legacy = Path(self.temp_dir.name) / ".github-app.json"
        legacy.write_text('{"client_id":"legacy-client-id"}', encoding="utf-8")
        with patch.dict("os.environ", {"GITHUB_APP_CLIENT_ID": "environment-client-id"}):
            with self.assertRaises(store.PreferencesError) as error:
                store.get_client_id(self.path)
        self.assertEqual("github_app_unconfigured", error.exception.code)

    def test_concurrent_updates_keep_both_fields(self):
        self.path.write_text(
            json.dumps({"github_app_client_id": "public-client-id"}),
            encoding="utf-8",
        )
        failures = []

        def save(changes):
            try:
                store.save_preferences(changes, self.path)
            except Exception as error:  # surfaced in the main test thread
                failures.append(error)

        threads = [
            threading.Thread(target=save, args=({"repo": "owner/repo"},)),
            threading.Thread(target=save, args=({"base": "main"},)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual([], failures)
        saved = store.read_preferences(self.path)
        self.assertEqual("owner/repo", saved["repo"])
        self.assertEqual("main", saved["base"])
        self.assertEqual("public-client-id", saved["github_app_client_id"])

    def test_external_change_during_save_is_reported_without_replacement(self):
        self.path.write_text('{"repo":"before"}', encoding="utf-8")
        original_fsync = store.os.fsync
        changed = False

        def update_original_then_fsync(descriptor):
            nonlocal changed
            if not changed:
                self.path.write_text('{"repo":"external"}', encoding="utf-8")
                changed = True
            return original_fsync(descriptor)

        with patch.object(store.os, "fsync", side_effect=update_original_then_fsync):
            with self.assertRaises(store.PreferencesError) as error:
                store.save_preferences({"base": "main"}, self.path)

        self.assertEqual("preferences_conflict", error.exception.code)
        self.assertEqual({"repo": "external"}, json.loads(self.path.read_text(encoding="utf-8")))
        self.assertEqual([], list(Path(self.temp_dir.name).glob(".preferences.*.tmp")))

    def test_atomic_replace_failure_preserves_existing_file(self):
        self.path.write_text('{"repo":"before"}', encoding="utf-8")
        with patch.object(store.os, "replace", side_effect=OSError("synthetic failure")):
            with self.assertRaises(store.PreferencesError) as error:
                store.save_preferences({"base": "main"}, self.path)

        self.assertEqual("preferences_save_failed", error.exception.code)
        self.assertEqual({"repo": "before"}, json.loads(self.path.read_text(encoding="utf-8")))
        self.assertEqual([], list(Path(self.temp_dir.name).glob(".preferences.*.tmp")))


if __name__ == "__main__":
    unittest.main()
