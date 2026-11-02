#!/usr/bin/env python3
"""One-time: scramble every Google connection key still in the old plain form, in every workspace. Dry run by default; --apply writes.
Each key is checked to open back to exactly the same content before its file is replaced. Needs LK_TOKEN_KEY.

    python3 scripts/seal_google_keys.py profiles/data [--apply]
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from google_oauth.sealed import MARK, seal, unseal  # noqa: E402


def main(data: Path, apply: bool) -> None:
    if not os.getenv("LK_TOKEN_KEY", "").strip():
        sys.exit("LK_TOKEN_KEY is not set: nothing done.")
    done = 0
    for f in sorted(data.glob("*/google/accounts/*.json")) + sorted(data.glob("*/google/members/*.json")) + sorted(data.glob("*/google/tokens.json")):
        stored = json.loads(f.read_text())
        if MARK in stored:
            print("already sealed:", f.parent.name)
            continue
        sealed = seal(stored)
        if MARK not in sealed or unseal(sealed) != stored:
            sys.exit(f"Stopped: a key would not open back correctly in {f.parent.name}. Nothing more was changed.")
        done += 1
        print(("sealed:" if apply else "would seal:"), f.parent.name, "file", done)
        if apply:
            tmp = f.with_name(f.name + ".tmp")
            tmp.write_text(json.dumps(sealed, indent=2))
            os.chmod(tmp, 0o600)
            if os.geteuid() == 0:
                st = f.stat()
                os.chown(tmp, st.st_uid, st.st_gid)
            tmp.replace(f)
    print(f"{done} key file(s) {'sealed' if apply else 'would be sealed (dry run; --apply to write)'}")


if __name__ == "__main__":
    main(Path(sys.argv[1]), "--apply" in sys.argv[2:])
