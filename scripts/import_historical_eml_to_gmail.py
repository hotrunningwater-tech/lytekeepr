from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from gmail.client import get_message, get_profile, import_message
from gmail.errors import GmailSyncError
from gmail.sync import get_active_account_id
from google_oauth.errors import GoogleOAuthError
from google_oauth.store import account_has_refresh_token

SOURCE_DIR = APP_DIR / "simulated-case" / "1_actual_demo_data" / "Historical_Emails"
MANIFEST_PATH = APP_DIR / "demo_state" / "gmail-eml-import-manifest.json"


@dataclass
class ImportOutcome:
    imported: int = 0
    skipped: int = 0
    failed: int = 0


@dataclass
class VerifyOutcome:
    found: int = 0
    missing: int = 0


def historical_eml_files(source_dir: Path = SOURCE_DIR) -> list[Path]:
    return sorted(path for path in source_dir.glob("*.eml") if path.is_file())


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def extract_message_id(raw_message: bytes) -> str:
    message = BytesParser(policy=policy.default).parsebytes(raw_message)
    return (message.get("Message-Id") or message.get("Message-ID") or "").strip()


def load_manifest(path: Path = MANIFEST_PATH) -> dict:
    if not path.is_file():
        return {"imports": {}}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"imports": {}}


def save_manifest(manifest: dict, path: Path = MANIFEST_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2))


def manifest_key_for_path(path: Path) -> str:
    return str(path.relative_to(APP_DIR))


def should_skip_import(manifest: dict, eml_path: Path, checksum: str) -> bool:
    entry = (manifest.get("imports") or {}).get(manifest_key_for_path(eml_path))
    return bool(entry and entry.get("sha256") == checksum and entry.get("status") == "imported")


def record_import_result(
    manifest: dict,
    eml_path: Path,
    *,
    checksum: str,
    message_id: str,
    status: str,
    imported_gmail_id: str | None = None,
    error: str | None = None,
) -> None:
    imports = manifest.setdefault("imports", {})
    imports[manifest_key_for_path(eml_path)] = {
        "sha256": checksum,
        "message_id": message_id,
        "status": status,
        "gmail_message_id": imported_gmail_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "error": error,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import the 25 historical .eml files into the connected Gmail test account.")
    parser.add_argument("--dry-run", action="store_true", help="List what would be imported without calling Gmail.")
    parser.add_argument("--yes", action="store_true", help="Run the real import.")
    parser.add_argument("--verify", action="store_true", help="Verify imported Gmail message ids against the connected Gmail account.")
    parser.add_argument("--manifest", type=Path, default=MANIFEST_PATH, help="Override the manifest path.")
    return parser.parse_args()


def verify_imported_messages(manifest_path: Path) -> int:
    account_id = get_active_account_id()
    if not account_id:
        print("No active Gmail account profile is selected.")
        return 1
    if not account_has_refresh_token(account_id):
        print(f"Active Gmail profile '{account_id}' does not have a live OAuth token. Connect Gmail for that profile first.")
        return 1

    manifest = load_manifest(manifest_path)
    imports = manifest.get("imports") or {}
    imported_entries = [(path, entry) for path, entry in imports.items() if entry.get("status") == "imported" and entry.get("gmail_message_id")]
    if not imported_entries:
        print("No imported Gmail message ids were found in the manifest.")
        return 1

    try:
        profile = get_profile()
    except (GoogleOAuthError, GmailSyncError) as exc:
        print(f"Could not resolve the connected Gmail account: {exc}")
        return 1

    connected_email = profile.get("emailAddress") or account_id
    outcome = VerifyOutcome()
    label_counts: dict[str, int] = {}

    print(f"Connected Gmail account: {connected_email}")
    print(f"Verifying {len(imported_entries)} imported Gmail message ids from manifest")

    for relative_path, entry in imported_entries:
        gmail_message_id = entry["gmail_message_id"]
        try:
            message = get_message(gmail_message_id)
        except Exception as exc:  # noqa: BLE001
            outcome.missing += 1
            print(f"MISSING  {relative_path} :: gmail_id={gmail_message_id} :: {exc}")
            continue

        outcome.found += 1
        payload = message.get("payload") or {}
        headers = payload.get("headers") or []
        subject = next((h.get("value", "") for h in headers if h.get("name", "").lower() == "subject"), "")
        date_header = next((h.get("value", "") for h in headers if h.get("name", "").lower() == "date"), "")
        label_ids = message.get("labelIds") or []
        for label in label_ids:
            label_counts[label] = label_counts.get(label, 0) + 1
        print(
            f"FOUND  {relative_path} :: gmail_id={gmail_message_id} :: labels={','.join(label_ids) or '(none)'} :: subject={subject} :: date={date_header}"
        )

    print("")
    print(f"Found: {outcome.found}")
    print(f"Missing: {outcome.missing}")
    print(f"Label counts: {json.dumps(label_counts, sort_keys=True)}")
    return 0 if outcome.missing == 0 else 2


def run_import(dry_run: bool, manifest_path: Path) -> int:
    files = historical_eml_files()
    if len(files) != 25:
        print(f"Expected 25 historical .eml files, found {len(files)}.")
        return 1

    account_id = get_active_account_id()
    if not account_id:
        print("No active Gmail account profile is selected.")
        return 1
    if not account_has_refresh_token(account_id):
        print(f"Active Gmail profile '{account_id}' does not have a live OAuth token. Connect Gmail for that profile first.")
        return 1

    connected_email = account_id
    if not dry_run:
        try:
            profile = get_profile()
        except (GoogleOAuthError, GmailSyncError) as exc:
            print(f"Could not resolve the connected Gmail account: {exc}")
            return 1
        connected_email = profile.get("emailAddress") or account_id
    manifest = load_manifest(manifest_path)
    outcome = ImportOutcome()

    print(f"Target Gmail account: {connected_email}")
    print(f"Historical source set: {len(files)} files from {SOURCE_DIR.relative_to(APP_DIR)}")
    if dry_run:
        print("Mode: dry-run")
    elif not dry_run:
        print("Mode: real import")

    for eml_path in files:
        raw_message = eml_path.read_bytes()
        checksum = sha256_bytes(raw_message)
        message_id = extract_message_id(raw_message)
        relative_path = manifest_key_for_path(eml_path)

        if should_skip_import(manifest, eml_path, checksum):
            outcome.skipped += 1
            print(f"SKIP  {relative_path}")
            continue

        if dry_run:
            outcome.imported += 1
            print(f"PLAN  {relative_path}")
            continue

        try:
            result = import_message(raw_message, internal_date_source="dateHeader", never_mark_spam=True)
        except Exception as exc:  # noqa: BLE001
            outcome.failed += 1
            record_import_result(
                manifest,
                eml_path,
                checksum=checksum,
                message_id=message_id,
                status="failed",
                error=str(exc),
            )
            print(f"FAIL  {relative_path} :: {exc}")
            continue

        outcome.imported += 1
        record_import_result(
            manifest,
            eml_path,
            checksum=checksum,
            message_id=message_id,
            status="imported",
            imported_gmail_id=result.get("id"),
        )
        print(f"IMPORTED  {relative_path}")

    if not dry_run:
        save_manifest(manifest, manifest_path)

    print("")
    print(f"Imported: {outcome.imported}")
    print(f"Skipped: {outcome.skipped}")
    print(f"Failed: {outcome.failed}")
    return 0 if outcome.failed == 0 else 2


def main() -> int:
    args = parse_args()
    if args.verify:
        return verify_imported_messages(args.manifest)
    dry_run = args.dry_run or not args.yes
    return run_import(dry_run=dry_run, manifest_path=args.manifest)


if __name__ == "__main__":
    raise SystemExit(main())
