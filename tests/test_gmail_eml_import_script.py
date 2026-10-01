import tempfile
import unittest
from pathlib import Path

from scripts.import_historical_eml_to_gmail import (
    APP_DIR,
    historical_eml_files,
    manifest_key_for_path,
    record_import_result,
    should_skip_import,
)


class GmailEmlImportScriptTest(unittest.TestCase):
    def test_historical_source_set_contains_25_eml_files(self):
        self.assertEqual(len(historical_eml_files()), 25)

    def test_manifest_skip_only_for_matching_imported_checksum(self):
        with tempfile.TemporaryDirectory() as tempdir:
            temp_path = Path(tempdir)
            eml_path = APP_DIR / "simulated-case" / "1_actual_demo_data" / "Historical_Emails" / "001_2026-01-06_initial_case_summary.eml"
            manifest = {"imports": {}}

            record_import_result(
                manifest,
                eml_path,
                checksum="abc123",
                message_id="<message-id>",
                status="imported",
                imported_gmail_id="gmail-id",
            )
            self.assertTrue(should_skip_import(manifest, eml_path, "abc123"))
            self.assertFalse(should_skip_import(manifest, eml_path, "different"))

            key = manifest_key_for_path(eml_path)
            self.assertIn("Historical_Emails", key)


if __name__ == "__main__":
    unittest.main()
