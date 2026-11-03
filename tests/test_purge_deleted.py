"""Deleted or closed workspaces are erased for good after 30 days: only old ones, only folders whose name carries the date."""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from profiles import store  # noqa: E402


class PurgeDeletedTest(unittest.TestCase):
    def test_only_old_dated_folders_go(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(store, "PROFILE_DELETED_ROOT", Path(tmp)):
            def folder(days_ago):
                f = Path(tmp) / f"0002-ws-x-{datetime.now() - timedelta(days=days_ago):%Y%m%d-%H%M%S}"
                f.mkdir()
                return f
            old, recent, no_date = folder(31), folder(29), Path(tmp) / "keep-me"
            no_date.mkdir()
            self.assertEqual(store.purge_deleted(), [old.name])
            self.assertFalse(old.exists())
            self.assertTrue(recent.exists() and no_date.exists())

    def test_no_folder_is_fine(self):
        with mock.patch.object(store, "PROFILE_DELETED_ROOT", Path("/nonexistent-lk-folder")):
            self.assertEqual(store.purge_deleted(), [])


if __name__ == "__main__":
    unittest.main()
