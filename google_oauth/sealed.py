"""Google connection keys are scrambled before they are written to disk, so a copied file (or a backup) alone is useless.

The unscrambling key is LK_TOKEN_KEY in the server's environment file, outside the app and data folders. Without it (a developer's
machine) keys stay plain, as they always were. A key still in the old plain form is read as it is and scrambled the next time it is saved;
scripts/seal_google_keys.py does all of them at once.
"""

import json
import logging
import os

from cryptography.fernet import Fernet, InvalidToken

MARK = "sealed_v1"


def _box() -> Fernet | None:
    key = os.getenv("LK_TOKEN_KEY", "").strip()
    return Fernet(key.encode()) if key else None


def seal(data: dict) -> dict:
    box = _box()
    return {MARK: box.encrypt(json.dumps(data).encode()).decode()} if box else data


def unseal(stored: dict | None) -> dict | None:
    """The real keys; None when they can't be opened (the wrong LK_TOKEN_KEY): the person is asked to connect Google again."""
    if not isinstance(stored, dict) or MARK not in stored:
        return stored
    box = _box()
    try:
        if not box:
            raise InvalidToken
        return json.loads(box.decrypt(stored[MARK].encode()).decode())
    except (InvalidToken, ValueError):
        logging.getLogger(__name__).error("A Google connection key could not be opened: is LK_TOKEN_KEY right?")
        return None
