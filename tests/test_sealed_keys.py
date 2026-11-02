"""Google connection keys: scrambled on disk when LK_TOKEN_KEY is set; old plain ones still work; the wrong key never crashes, it asks to reconnect."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cryptography.fernet import Fernet  # noqa: E402

from google_oauth import sealed, store  # noqa: E402

KEY, OTHER = Fernet.generate_key().decode(), Fernet.generate_key().decode()
SECRET = {"refresh_token": "very-secret-token", "access_token": "abc"}


class SealedKeysTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = store.TOKENS_DIR, store.STORE_PATH
        store.TOKENS_DIR, store.STORE_PATH = Path(self.tmp.name) / "accounts", Path(self.tmp.name) / "tokens.json"

    def tearDown(self):
        store.TOKENS_DIR, store.STORE_PATH = self.old
        self.tmp.cleanup()

    def test_the_file_is_scrambled_and_opens_again(self):
        with mock.patch.dict(os.environ, {"LK_TOKEN_KEY": KEY}):
            store.save_tokens(SECRET, account_id="a@b.c")
            text = store.account_tokens_path("a@b.c").read_text()
            self.assertNotIn("very-secret-token", text)
            self.assertIn(sealed.MARK, text)
            self.assertEqual(oct(store.account_tokens_path("a@b.c").stat().st_mode)[-3:], "600")
            self.assertEqual(store.load_tokens("a@b.c"), SECRET)

    def test_an_old_plain_key_still_works(self):
        store.account_tokens_path("a@b.c").parent.mkdir(parents=True)
        store.account_tokens_path("a@b.c").write_text(json.dumps(SECRET))
        with mock.patch.dict(os.environ, {"LK_TOKEN_KEY": KEY}):
            self.assertEqual(store.load_tokens("a@b.c"), SECRET)

    def test_wrong_or_missing_key_means_reconnect_not_a_crash(self):
        with mock.patch.dict(os.environ, {"LK_TOKEN_KEY": KEY}):
            store.save_tokens(SECRET, account_id="a@b.c")
        with mock.patch.dict(os.environ, {"LK_TOKEN_KEY": OTHER}):
            self.assertIsNone(store.load_tokens("a@b.c"))
        with mock.patch.dict(os.environ, {"LK_TOKEN_KEY": ""}):
            self.assertIsNone(store.load_tokens("a@b.c"))

    def test_no_key_on_a_developer_machine_stays_plain(self):
        with mock.patch.dict(os.environ, {"LK_TOKEN_KEY": ""}):
            store.save_tokens(SECRET, account_id="a@b.c")
            self.assertEqual(json.loads(store.account_tokens_path("a@b.c").read_text()), SECRET)


if __name__ == "__main__":
    unittest.main()
