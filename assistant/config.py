import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

MODULE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = MODULE_DIR / ".env"
_ENV_LOADED = False


def _ensure_env_loaded() -> None:
    global _ENV_LOADED
    if not _ENV_LOADED:
        load_dotenv(ENV_PATH)
        _ENV_LOADED = True


@dataclass
class AssistantConfig:
    api_key: str | None = None
    model: str = "gpt-5-mini"

    @classmethod
    def from_env(cls) -> "AssistantConfig":
        _ensure_env_loaded()
        return cls(
            api_key=os.getenv("OPENAI_API_KEY"),
            model=os.getenv("OPENAI_MODEL", "gpt-5-mini"),
        )
