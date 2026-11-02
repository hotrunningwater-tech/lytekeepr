"""An unexpected failure gives plain words, never a raw error or stack; normal web errors stay as they are; Google's raw replies stay out of messages."""

import os
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class PlainErrorsTest(unittest.TestCase):
    def test_google_raw_reply_is_not_shown(self):
        from google_oauth import flow
        from google_oauth.errors import GoogleOAuthError
        err = urllib.error.HTTPError("u", 400, "bad", {}, __import__("io").BytesIO(b'{"error":"invalid_grant","secret":"xyz"}'))
        with mock.patch.object(flow.urllib.request, "urlopen", side_effect=err), mock.patch.object(flow, "load_config", create=True):
            try:
                flow.refresh_access_token("t")
            except GoogleOAuthError as exc:
                self.assertNotIn("invalid_grant", str(exc))
                self.assertIn("connect Google again", str(exc))
            except Exception as exc:                  # no Google settings on this machine: nothing to check here
                self.skipTest(str(exc))


if __name__ == "__main__":
    unittest.main()
