import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .errors import ConfigurationError

MODULE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = MODULE_DIR.parent
DLYTE_ENV_PATH = PROJECT_ROOT / "experiment DLYTE" / ".env"
_ENV_LOADED = False


def _ensure_env_loaded() -> None:
    global _ENV_LOADED
    if not _ENV_LOADED:
        # DLYTE's own .env takes priority (it holds the keys for this
        # experiment); the project-root .env (used by ocr_scanned_pdf.py)
        # is loaded after as a fallback for anything not set there.
        load_dotenv(DLYTE_ENV_PATH, override=True)
        load_dotenv(PROJECT_ROOT / ".env")
        _ENV_LOADED = True


SUPPORTED_EXTENSIONS = {".pdf"}  # MVP: PDF only


@dataclass
class OCRServiceConfig:
    api_key: str | None = None
    provider: str = "google"
    language: str = "en"
    request_timeout_seconds: int = 120
    pdf_dpi: int = 300
    max_retries: int = 2

    # Storage is on by default with a working default location — no setup
    # required. Self-contained inside this module, not the project root,
    # so test output never mixes with real case files. Still overridable
    # via env var or by passing this directly.
    storage_root: Path = MODULE_DIR / "storage_data"

    @classmethod
    def from_env(cls) -> "OCRServiceConfig":
        _ensure_env_loaded()

        storage_root = os.getenv("OCR_SERVICE_STORAGE_ROOT")

        return cls(
            api_key=os.getenv("EDEN_AI_API_KEY"),
            provider=os.getenv("OCR_SERVICE_PROVIDER", "google"),
            language=os.getenv("OCR_SERVICE_LANGUAGE", "en"),
            request_timeout_seconds=int(os.getenv("OCR_SERVICE_TIMEOUT_SECONDS", "120")),
            pdf_dpi=int(os.getenv("OCR_SERVICE_PDF_DPI", "300")),
            max_retries=int(os.getenv("OCR_SERVICE_MAX_RETRIES", "2")),
            storage_root=Path(storage_root) if storage_root else (PROJECT_ROOT / "ocr-storage"),
        )

    def require_api_key(self) -> str:
        if not self.api_key:
            raise ConfigurationError(
                "EDEN_AI_API_KEY is not set. Set it in the project .env file "
                "or pass api_key explicitly via OCRServiceConfig."
            )
        return self.api_key
