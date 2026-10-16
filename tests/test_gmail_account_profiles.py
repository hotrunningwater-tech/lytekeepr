import tempfile
import unittest
from pathlib import Path

from gmail import storage as gmail_storage
from gmail import sync as gmail_sync
from google_oauth import store as oauth_store
from profiles import store as profile_store


class GmailAccountProfilesTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

        self.original_store_path = oauth_store.STORE_PATH
        self.original_tokens_dir = oauth_store.TOKENS_DIR
        self.original_active_account_path = oauth_store.ACTIVE_ACCOUNT_PATH
        self.original_storage_root = gmail_storage.STORAGE_ROOT
        self.original_profile_root = profile_store.PROFILE_ROOT
        self.original_profile_data_root = profile_store.PROFILE_DATA_ROOT
        self.original_registry_path = profile_store.REGISTRY_PATH
        self.original_active_profile_path = profile_store.ACTIVE_PROFILE_PATH

        oauth_store.STORE_PATH = self.root / "legacy-tokens.json"
        oauth_store.TOKENS_DIR = self.root / "oauth-accounts"
        oauth_store.ACTIVE_ACCOUNT_PATH = self.root / "active-account.json"
        gmail_storage.STORAGE_ROOT = self.root / "email-storage"
        profile_store.PROFILE_ROOT = self.root / "profiles"
        profile_store.PROFILE_DATA_ROOT = profile_store.PROFILE_ROOT / "data"
        profile_store.REGISTRY_PATH = profile_store.PROFILE_ROOT / "registry.json"
        profile_store.ACTIVE_PROFILE_PATH = profile_store.PROFILE_ROOT / "active-profile.json"

    def tearDown(self):
        oauth_store.STORE_PATH = self.original_store_path
        oauth_store.TOKENS_DIR = self.original_tokens_dir
        oauth_store.ACTIVE_ACCOUNT_PATH = self.original_active_account_path
        gmail_storage.STORAGE_ROOT = self.original_storage_root
        profile_store.PROFILE_ROOT = self.original_profile_root
        profile_store.PROFILE_DATA_ROOT = self.original_profile_data_root
        profile_store.REGISTRY_PATH = self.original_registry_path
        profile_store.ACTIVE_PROFILE_PATH = self.original_active_profile_path
        self.tempdir.cleanup()

    def test_saves_tokens_per_account_and_loads_active_account(self):
        oauth_store.save_tokens({"refresh_token": "token-a"}, account_id="a@example.com")
        oauth_store.save_tokens({"refresh_token": "token-b"}, account_id="b@example.com")
        oauth_store.set_active_account_id("b@example.com")

        self.assertEqual(oauth_store.load_tokens(), {"refresh_token": "token-b"})
        self.assertEqual(oauth_store.load_tokens("a@example.com"), {"refresh_token": "token-a"})
        self.assertEqual(oauth_store.list_token_account_ids(), ["a@example.com", "b@example.com"])

    def test_active_account_falls_back_to_local_profile_when_pointer_missing(self):
        gmail_storage.save_sync_state("z@example.com", {"messageCount": 3})
        gmail_storage.save_sync_state("a@example.com", {"messageCount": 2})

        self.assertEqual(gmail_sync.get_active_account_id(), "a@example.com")

    def test_active_account_prefers_explicit_pointer(self):
        gmail_storage.save_sync_state("a@example.com", {"messageCount": 2})
        gmail_storage.save_sync_state("z@example.com", {"messageCount": 3})
        oauth_store.set_active_account_id("z@example.com")

        self.assertEqual(gmail_sync.get_active_account_id(), "z@example.com")

    def test_empty_workspace_does_not_inherit_another_workspaces_account(self):
        profile_store.ensure_profile_bootstrap()
        oauth_store.set_active_account_id("stalepoetry@gmail.com")
        profile_store.create_profile("New account", profile_id="ws-new")
        with profile_store.use_profile("ws-new"):
            self.assertIsNone(oauth_store.load_active_account_id())
            self.assertIsNone(gmail_sync.get_active_account_id())
            self.assertFalse(oauth_store.has_refresh_token())

    def test_empty_account_directory_does_not_count_as_local_mailbox_state(self):
        profile_store.ensure_profile_bootstrap()
        profile_store.create_profile("New account", profile_id="ws-new")
        with profile_store.use_profile("ws-new"):
            empty_messages_dir = gmail_storage.account_dir("stalepoetry@gmail.com") / "messages"
            empty_messages_dir.mkdir(parents=True, exist_ok=True)

            self.assertEqual(gmail_storage.list_account_ids(), ["stalepoetry@gmail.com"])
            self.assertEqual(gmail_storage.list_account_ids_with_state(), [])
            self.assertIsNone(gmail_sync.get_active_account_id())


if __name__ == "__main__":
    unittest.main()
