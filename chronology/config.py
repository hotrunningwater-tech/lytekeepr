from pathlib import Path
from profiles.store import profile_component_dir

MODULE_DIR = Path(__file__).resolve().parent.parent
STORAGE_DIR = None


def get_storage_dir() -> Path:
    if STORAGE_DIR is not None:
        return STORAGE_DIR
    return profile_component_dir("chronology-data")

CATEGORIES = {
    "court_procedural": "Court and procedural events",
    "confirmed_action": "Confirmed or completed actions",
    "deadline_request": "Deadlines, requests, and non-compliance",
    "communication": "Important communications",
    "case_fact": "Important case facts and changes",
}

NATURE_OF_CLAIM_VALUES = {"confirmed_fact", "proposed", "alleged", "disputed"}
IMPORTANCE_LEVELS = {"high", "medium", "low"}
CONFIDENCE_LEVELS = {"high", "medium", "low"}

EMAIL_BATCH_SIZE = 8
