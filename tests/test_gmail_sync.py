import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gmail import storage as gmail_storage
from gmail import sync as gmail_sync
from profiles import store as profile_store


class GmailSyncWindowTest(unittest.TestCase):
    def test_full_window_query_omits_newer_than_when_unbounded(self):
        config = gmail_sync.GmailSyncConfig(sync_window_days=None)

        with mock.patch.object(gmail_sync.client, "list_message_ids", return_value={"messages": []}) as list_ids:
            gmail_sync._list_full_window_ids(config)

        query = list_ids.call_args.args[0]
        self.assertNotIn("newer_than:", query)
        self.assertIn("-in:spam", query)
        self.assertIn("-in:trash", query)


class GmailSyncRebuildTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.original_storage_root = gmail_storage.STORAGE_ROOT
        self.original_profile_root = profile_store.PROFILE_ROOT
        self.original_profile_data_root = profile_store.PROFILE_DATA_ROOT
        self.original_registry_path = profile_store.REGISTRY_PATH
        self.original_active_profile_path = profile_store.ACTIVE_PROFILE_PATH
        gmail_storage.STORAGE_ROOT = Path(self.tempdir.name) / "email-storage"
        profile_store.PROFILE_ROOT = Path(self.tempdir.name) / "profiles"
        profile_store.PROFILE_DATA_ROOT = profile_store.PROFILE_ROOT / "data"
        profile_store.REGISTRY_PATH = profile_store.PROFILE_ROOT / "registry.json"
        profile_store.ACTIVE_PROFILE_PATH = profile_store.PROFILE_ROOT / "active-profile.json"

    def tearDown(self):
        gmail_storage.STORAGE_ROOT = self.original_storage_root
        profile_store.PROFILE_ROOT = self.original_profile_root
        profile_store.PROFILE_DATA_ROOT = self.original_profile_data_root
        profile_store.REGISTRY_PATH = self.original_registry_path
        profile_store.ACTIVE_PROFILE_PATH = self.original_active_profile_path

    def test_scan_forces_full_rebuild_when_history_exists_but_local_mailbox_state_missing(self):
        account_id = "tester@example.com"
        gmail_storage.save_sync_state(account_id, {"historyId": "18065"})

        full_window_ids = ["m1", "m2"]
        def fake_get_message(message_id):
            return {
                "id": message_id,
                "threadId": f"thread-{message_id}",
                "labelIds": ["INBOX"],
                "snippet": f"snippet-{message_id}",
                "payload": {"headers": []},
                "internalDate": "1760000000000",
                "historyId": "18070",
            }

        def fake_normalize_message(raw, _account_id):
            message_id = raw["id"]
            return {
                "id": message_id,
                "threadId": raw["threadId"],
                "labelIds": ["INBOX"],
                "snippet": raw["snippet"],
                "subject": f"Subject {message_id}",
                "receivedAt": "2026-01-06T22:42:00+00:00",
                "from": {"name": "Sender", "email": "sender@example.com"},
                "to": [],
                "hasAttachments": False,
                "isUnread": True,
                "historyId": "18070",
            }

        with (
            mock.patch.object(gmail_sync.client, "get_profile", return_value={"emailAddress": account_id, "historyId": "18099"}),
            mock.patch.object(gmail_sync, "_list_full_window_ids", return_value=full_window_ids) as full_scan,
            mock.patch.object(gmail_sync, "_list_history_ids") as history_scan,
            mock.patch.object(gmail_sync.client, "get_message", side_effect=fake_get_message),
            mock.patch.object(gmail_sync, "normalize_message", side_effect=fake_normalize_message),
        ):
            result = gmail_sync.scan_for_candidates(lambda messages: {m["id"]: {"is_relevant": False, "reason": "test"} for m in messages})

        full_scan.assert_called_once()
        history_scan.assert_not_called()
        self.assertTrue(result["fell_back_to_full_scan"])
        self.assertEqual("local_mailbox_state_missing", result["reason"])
        self.assertEqual(2, result["scanned"])
        self.assertEqual(2, len(gmail_storage.load_scan_state(account_id)))


if __name__ == "__main__":
    unittest.main()
