import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from profiles import store as profile_store


class ProfileStoreTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

        self.patchers = [
            mock.patch.object(profile_store, "APP_DIR", self.root),
            mock.patch.object(profile_store, "PROFILE_ROOT", self.root / "profiles"),
            mock.patch.object(profile_store, "PROFILE_DATA_ROOT", self.root / "profiles" / "data"),
            mock.patch.object(profile_store, "REGISTRY_PATH", self.root / "profiles" / "registry.json"),
            mock.patch.object(profile_store, "ACTIVE_PROFILE_PATH", self.root / "profiles" / "active-profile.json"),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_bootstrap_migrates_legacy_state_into_default_profile(self):
        legacy_email = self.root / "email-storage" / "accounts" / "alpha@example.com"
        legacy_email.mkdir(parents=True, exist_ok=True)
        (legacy_email / "sync-state.json").write_text(json.dumps({"messageCount": 2}))
        (self.root / "chronology-data").mkdir(parents=True, exist_ok=True)
        (self.root / "chronology-data" / "events.json").write_text(json.dumps({"evt-1": {"id": "evt-1"}}))
        (self.root / "google_oauth").mkdir(parents=True, exist_ok=True)
        (self.root / "google_oauth" / "active-account.json").write_text(json.dumps({"account_id": "alpha@example.com"}))

        profile_store.ensure_profile_bootstrap()

        active_profile_id = profile_store.get_active_profile_id()
        self.assertEqual("profile-default", active_profile_id)
        self.assertEqual("0001", profile_store.get_active_profile().get("storage_id"))
        self.assertEqual("default", profile_store.get_active_profile().get("shell_variant"))
        migrated_email_root = profile_store.get_active_profile_root() / "email-storage" / "accounts" / "alpha@example.com"
        self.assertTrue(migrated_email_root.is_dir())
        self.assertFalse((self.root / "email-storage").exists())

    def test_create_switch_and_count_profiles(self):
        profile_store.ensure_profile_bootstrap()
        profile_store.create_profile("Alpha", profile_id="profile-alpha", is_demo_profile=True)
        profile_store.create_profile("Beta", profile_id="profile-beta", is_demo_profile=True)
        profile_store.set_active_profile_id("profile-alpha")

        alpha_root = profile_store.get_active_profile_root()
        (alpha_root / "email-storage" / "accounts" / "alpha@example.com").mkdir(parents=True, exist_ok=True)
        (alpha_root / "email-storage" / "accounts" / "alpha@example.com" / "index.json").write_text(json.dumps({"m1": {"id": "m1"}}))
        (alpha_root / "ocr-storage" / "jobs" / "job-1" / "results").mkdir(parents=True, exist_ok=True)
        (alpha_root / "ocr-storage" / "jobs" / "job-1" / "results" / "result.json").write_text(json.dumps({"job_id": "job-1"}))

        beta_root = profile_store.get_profile_root("profile-beta")
        (beta_root / "chronology-data").mkdir(parents=True, exist_ok=True)
        (beta_root / "chronology-data" / "events.json").write_text(json.dumps({"evt-1": {"id": "evt-1"}, "evt-2": {"id": "evt-2"}}))

        alpha_counts = profile_store.profile_counts("profile-alpha")
        beta_counts = profile_store.profile_counts("profile-beta")

        self.assertEqual(1, alpha_counts["saved_emails"])
        self.assertEqual(1, alpha_counts["scanned_documents"])
        self.assertEqual(0, alpha_counts["chronology_events"])
        self.assertEqual(2, beta_counts["chronology_events"])

        alpha_profile = next(profile for profile in profile_store.list_profiles() if profile["profile_id"] == "profile-alpha")
        beta_profile = next(profile for profile in profile_store.list_profiles() if profile["profile_id"] == "profile-beta")
        self.assertEqual("0002", alpha_profile["storage_id"])
        self.assertEqual("0003", beta_profile["storage_id"])
        self.assertEqual("variant_1", alpha_profile["shell_variant"])
        self.assertEqual("variant_2", beta_profile["shell_variant"])

    def test_reset_profile_local_state_only_clears_target_profile(self):
        profile_store.ensure_profile_bootstrap()
        profile_store.create_profile("Alpha", profile_id="profile-alpha", is_demo_profile=True)
        profile_store.create_profile("Beta", profile_id="profile-beta", is_demo_profile=True)

        alpha_docs = profile_store.get_profile_root("profile-alpha") / "ocr-storage" / "jobs" / "job-a"
        beta_docs = profile_store.get_profile_root("profile-beta") / "ocr-storage" / "jobs" / "job-b"
        alpha_docs.mkdir(parents=True, exist_ok=True)
        beta_docs.mkdir(parents=True, exist_ok=True)

        profile_store.reset_profile_local_state("profile-alpha")

        self.assertFalse((profile_store.get_profile_root("profile-alpha") / "ocr-storage").exists())
        self.assertTrue((profile_store.get_profile_root("profile-beta") / "ocr-storage").exists())

    def test_demo_profiles_get_first_three_shell_variants_in_order(self):
        profile_store.ensure_profile_bootstrap()

        first = profile_store.create_profile("First", profile_id="profile-first", is_demo_profile=True)
        second = profile_store.create_profile("Second", profile_id="profile-second", is_demo_profile=True)
        third = profile_store.create_profile("Third", profile_id="profile-third", is_demo_profile=True)
        fourth = profile_store.create_profile("Fourth", profile_id="profile-fourth", is_demo_profile=True)

        self.assertEqual("variant_1", first["shell_variant"])
        self.assertEqual("variant_2", second["shell_variant"])
        self.assertEqual("variant_3", third["shell_variant"])
        self.assertEqual("default", fourth["shell_variant"])

    def test_bootstrap_assigns_shell_variants_to_existing_demo_profiles(self):
        profile_store._write_json(
            profile_store.REGISTRY_PATH,
            {
                "version": 1,
                "profiles": {
                    "profile-default": {
                        "profile_id": "profile-default",
                        "display_name": "Default profile",
                        "storage_id": "0001",
                        "created_at": "2026-08-01T00:00:00+00:00",
                        "last_active_at": "2026-08-01T00:00:00+00:00",
                        "attached_gmail_account_ids": [],
                        "active_gmail_account_id": None,
                        "is_demo_profile": False,
                    },
                    "profile-alpha": {
                        "profile_id": "profile-alpha",
                        "display_name": "Alpha",
                        "storage_id": "0002",
                        "created_at": "2026-08-01T00:00:00+00:00",
                        "last_active_at": "2026-08-01T00:00:00+00:00",
                        "attached_gmail_account_ids": [],
                        "active_gmail_account_id": None,
                        "is_demo_profile": True,
                    },
                    "profile-beta": {
                        "profile_id": "profile-beta",
                        "display_name": "Beta",
                        "storage_id": "0003",
                        "created_at": "2026-08-01T00:00:00+00:00",
                        "last_active_at": "2026-08-01T00:00:00+00:00",
                        "attached_gmail_account_ids": [],
                        "active_gmail_account_id": None,
                        "is_demo_profile": True,
                    },
                },
            },
        )

        profiles = {profile["profile_id"]: profile for profile in profile_store.list_profiles()}

        self.assertEqual("default", profiles["profile-default"]["shell_variant"])
        self.assertEqual("variant_1", profiles["profile-alpha"]["shell_variant"])
        self.assertEqual("variant_2", profiles["profile-beta"]["shell_variant"])

    def test_existing_slug_folder_migrates_to_numeric_storage_id(self):
        profile_store.PROFILE_DATA_ROOT.mkdir(parents=True, exist_ok=True)
        legacy_profile_root = profile_store.PROFILE_DATA_ROOT / "profile-alpha"
        (legacy_profile_root / "ocr-storage" / "jobs" / "job-a").mkdir(parents=True, exist_ok=True)
        profile_store._write_json(
            profile_store.REGISTRY_PATH,
            {
                "version": 1,
                "profiles": {
                    "profile-alpha": {
                        "profile_id": "profile-alpha",
                        "display_name": "Alpha",
                        "created_at": "2026-08-01T00:00:00+00:00",
                        "last_active_at": "2026-08-01T00:00:00+00:00",
                        "attached_gmail_account_ids": [],
                        "active_gmail_account_id": None,
                        "is_demo_profile": True,
                    }
                },
            },
        )

        profile_store.ensure_profile_bootstrap()

        profile = profile_store.get_active_profile()
        self.assertEqual("0001", profile["storage_id"])
        self.assertFalse(legacy_profile_root.exists())
        self.assertTrue((profile_store.PROFILE_DATA_ROOT / "0001" / "ocr-storage" / "jobs" / "job-a").exists())


if __name__ == "__main__":
    unittest.main()
