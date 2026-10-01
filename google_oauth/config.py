import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .errors import GoogleOAuthError

MODULE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = MODULE_DIR / ".env"
_ENV_LOADED = False


def _ensure_env_loaded() -> None:
    global _ENV_LOADED
    if not _ENV_LOADED:
        load_dotenv(ENV_PATH)
        _ENV_LOADED = True


@dataclass
class GoogleOAuthConfig:
    client_id: str | None = None
    client_secret: str | None = None
    redirect_uri: str | None = None

    @classmethod
    def from_env(cls) -> "GoogleOAuthConfig":
        _ensure_env_loaded()
        return cls(
            client_id=os.getenv("GOOGLE_CLIENT_ID"),
            client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
            redirect_uri=os.getenv("GOOGLE_REDIRECT_URI"),
        )

    def require(self) -> "GoogleOAuthConfig":
        missing = [
            name
            for name, value in (
                ("GOOGLE_CLIENT_ID", self.client_id),
                ("GOOGLE_CLIENT_SECRET", self.client_secret),
                ("GOOGLE_REDIRECT_URI", self.redirect_uri),
            )
            if not value
        ]
        if missing:
            raise GoogleOAuthError(
                f"Missing {', '.join(missing)} in experiment DLYTE/.env."
            )
        return self
