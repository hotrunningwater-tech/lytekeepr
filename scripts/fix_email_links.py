#!/usr/bin/env python3
"""One-time fix: emails that came with only an HTML part got a plain-text copy with every link dropped (Vicky's "video" link).

Redoes that copy with gmail.parser.html_to_text (links kept as "video (https://...)") for the saved emails, their index
preview, and the CRM messages table. Only emails whose text was made from the HTML (or is empty) are touched; an email that
came with its own plain text stays as it is. Dry run by default; --apply writes.

    python3 scripts/fix_email_links.py profiles/data/0001 [--apply]
"""

import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gmail.parser import html_to_text  # noqa: E402


def old_text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


def main(data: Path, apply: bool) -> None:
    db = sqlite3.connect(data / "crm" / "lytekeepr.db")
    changed = 0
    for account in sorted((data / "email-storage" / "accounts").iterdir()):
        index_file = account / "index.json"
        index = json.loads(index_file.read_text()) if index_file.is_file() else {}
        for f in sorted((account / "messages").glob("*.json")):
            m = json.loads(f.read_text())
            html, text = m.get("htmlBody") or "", m.get("textBody") or ""
            if not html or (text.strip() and text != old_text(html)):
                continue
            new = html_to_text(html)
            if new == text:
                continue
            changed += 1
            print(f"{account.name} {m['id']} {m.get('subject', '')!r}: {len(re.findall('https?://', new))} links")
            if not apply:
                continue
            old_snippet = re.sub(r"\s+", " ", text)[:200]
            m["textBody"] = new
            if m.get("snippet", "") == old_snippet:
                m["snippet"] = re.sub(r"\s+", " ", new)[:200]
                if m["id"] in index:
                    index[m["id"]]["snippet"] = m["snippet"]
            tmp = f.with_suffix(".tmp")
            tmp.write_text(json.dumps(m, indent=2))
            tmp.replace(f)
            subject = m.get("subject") or ""
            for body in (text, m.get("snippet") or ""):
                was = f"Subject: {subject}\n\n{body}" if subject else body
                db.execute("UPDATE messages SET message_text = ? WHERE channel = 'email' AND external_message_id = ? AND message_text = ?",
                           ((f"Subject: {subject}\n\n{new}" if subject else new), m["id"], was))
        if apply and index:
            tmp = index_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(index, indent=2))
            tmp.replace(index_file)
    if apply:
        db.commit()
    print(f"{changed} emails {'fixed' if apply else 'would be fixed (dry run; --apply to write)'}")


if __name__ == "__main__":
    main(Path(sys.argv[1]), "--apply" in sys.argv[2:])
