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
        migrated_email_root = profile_store.get_active_profile_root() / "email-storage" / "accounts" / "alpha@example.com"
        self.assertTrue(migrated_email_root.is_dir())
        self.assertFalse((self.root / "email-storage").exists())

    def test_each_workspace_has_its_own_folder_and_a_block_returns_to_the_first(self):
        profile_store.ensure_profile_bootstrap()
        home = profile_store.get_active_profile_id()
        alpha = profile_store.create_profile("Alpha", profile_id="ws-alpha")
        beta = profile_store.create_profile("Beta", profile_id="ws-beta")
        self.assertNotEqual(alpha["storage_id"], beta["storage_id"])

        with profile_store.use_profile("ws-alpha"):
            self.assertEqual("ws-alpha", profile_store.get_active_profile_id())
            alpha_crm = profile_store.profile_component_dir("crm")
            with profile_store.use_profile("ws-beta"):
                beta_crm = profile_store.profile_component_dir("crm")
            self.assertEqual("ws-alpha", profile_store.get_active_profile_id())
        self.assertEqual(home, profile_store.get_active_profile_id())
        self.assertNotEqual(alpha_crm, beta_crm)
        self.assertEqual(profile_store.PROFILE_DATA_ROOT / alpha["storage_id"] / "crm", alpha_crm)

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
