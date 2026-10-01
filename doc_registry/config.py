from pathlib import Path
from profiles.store import profile_component_dir

MODULE_DIR = Path(__file__).resolve().parent.parent
STORAGE_DIR = None
OCR_STORAGE_ROOT = None


def get_storage_dir() -> Path:
    if STORAGE_DIR is not None:
        return STORAGE_DIR
    return profile_component_dir("document-registry")


def get_ocr_storage_root() -> Path:
    if OCR_STORAGE_ROOT is not None:
        return OCR_STORAGE_ROOT
    return profile_component_dir("ocr-storage")

DAILY_CHECK_INTERVAL_SECONDS = 24 * 60 * 60
