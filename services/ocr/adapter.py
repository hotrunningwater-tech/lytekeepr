"""
Integration adapter for the completed ocr_service package.

This module does not implement any OCR, PDF, or storage logic itself —
it only imports the existing ocr_service package (from the project
root, a sibling of this DLYTE experiment folder) and calls its one
public entry point, ocr_service.service.ocr_document(), pointed at a
storage location inside this DLYTE folder instead of the project root.

The original ocr_service package is not modified.
"""

import json
import logging
import re
import sys
import uuid
from dataclasses import replace
from io import BytesIO
from pathlib import Path

from assistant.config import AssistantConfig
from profiles.store import profile_component_dir
from services.openai.client import OpenAIServiceError, generate_text, generate_text_from_images

DLYTE_DIR = Path(__file__).resolve().parent.parent.parent
PROJECT_ROOT = DLYTE_DIR.parent
OCR_STORAGE_ROOT = None


def get_ocr_storage_root() -> Path:
    if OCR_STORAGE_ROOT is not None:
        return OCR_STORAGE_ROOT
    return profile_component_dir("ocr-storage")

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ocr_service.config import OCRServiceConfig  # noqa: E402
from ocr_service.models import OCRResult, PageResult  # noqa: E402
from ocr_service import normalize, preprocessing, provider, storage  # noqa: E402
from ocr_service.service import ocr_document  # noqa: E402
from PIL import Image  # noqa: E402

SUPPORTED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
SUPPORTED_UPLOAD_EXTENSIONS = {".pdf"} | SUPPORTED_IMAGE_EXTENSIONS
RECENT_SCAN_EXTENSIONS = SUPPORTED_UPLOAD_EXTENSIONS
FILENAME_MAX_STEM_LENGTH = 72
MIN_AI_NAMING_TEXT_LENGTH = 80
logger = logging.getLogger(__name__)
GENERIC_FILENAME_STEMS = {
    "document",
    "documents",
    "scan",
    "scanned",
    "scanned document",
    "scanned pages",
    "image",
    "photo",
    "file",
    "upload",
}


def scan_pdf(file_bytes: bytes, filename: str) -> dict:
    """Runs the existing OCR service against DLYTE's own storage folder
    and returns its structured OCRResult as a dict (see
    ocr_service.models.OCRResult.to_dict)."""
    storage_root = get_ocr_storage_root()
    storage_root.mkdir(parents=True, exist_ok=True)
    config = replace(OCRServiceConfig.from_env(), storage_root=storage_root)

    result = ocr_document(file_bytes, filename=filename, config=config)
    payload = result.to_dict()
    if payload.get("status") != "failed" and payload.get("job_id"):
        # Freeze the name the document-registry matches against, separate
        # from original_filename (which Rename is now free to change) — see
        # set_original_filename_label for why these can't be the same field.
        _freeze_match_filename(payload["job_id"], payload.get("original_filename", filename))
    return payload


def _freeze_match_filename(job_id: str, match_filename: str) -> None:
    result_path, payload = _load_result_payload(job_id)
    if not result_path or not payload:
        return
    payload["matchFilename"] = match_filename
    _save_result_payload(job_id, payload)


def _normalize_saved_filename(filename: str, *, default_extension: str = ".pdf") -> str:
    raw = (filename or "").strip()
    if not raw:
        raw = f"document{default_extension}"
    cleaned = "".join(char for char in raw if char not in '<>:"/\\|?*').strip().strip(".")
    if not cleaned:
        cleaned = f"document{default_extension}"
    suffix = Path(cleaned).suffix.lower()
    if not suffix:
        cleaned = f"{cleaned}{default_extension}"
    return cleaned


def _load_result_payload(job_id: str) -> tuple[Path, dict] | tuple[None, None]:
    result_path = get_ocr_storage_root() / "jobs" / job_id / "results" / "result.json"
    if not result_path.is_file():
        return None, None
    try:
        return result_path, json.loads(result_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None, None


def _save_result_payload(job_id: str, payload: dict) -> None:
    result_path = get_ocr_storage_root() / "jobs" / job_id / "results" / "result.json"
    result_path.write_text(json.dumps(payload, indent=2))


def set_original_filename_label(job_id: str, display_filename: str) -> None:
    """Corrects a freshly-created job's name immediately after scan —
    unlike rename_saved_document (a later, user-initiated rename), this
    also updates matchFilename, since at this point we're fixing the
    *true* original name, not changing it away from the truth. Needed for
    a converted .doc/.docx: scan_pdf was called with a temporary .pdf-named
    copy (see doc_convert.py), so both the display name and the frozen
    match name need correcting to the real original .docx name — the
    stored file itself stays a real .pdf so the browser viewer keeps
    working (see doc_registry.matcher, which matches on matchFilename)."""
    result_path, payload = _load_result_payload(job_id)
    if not result_path or not payload:
        return
    payload["original_filename"] = display_filename
    payload["matchFilename"] = display_filename
    _save_result_payload(job_id, payload)


def suggest_filename_from_ocr_text(full_text: str, fallback_filename: str) -> str:
    fallback = _normalize_saved_filename(fallback_filename)
    lines = [re.sub(r"\s+", " ", line).strip() for line in (full_text or "").splitlines()]
    candidates: list[str] = []
    for line in lines:
        if len(line) < 8 or len(line) > 96:
            continue
        if sum(char.isalpha() for char in line) < 5:
            continue
        lower = line.lower()
        if lower.startswith("page ") or lower.startswith("=== page"):
            continue
        if line in candidates:
            continue
        candidates.append(line)
        if len(candidates) >= 8:
            break

    if not candidates:
        return fallback

    priority_patterns = (
        "application",
        "affidavit",
        "statement",
        "notice",
        "order",
        "motion",
        "form",
        "certificate",
        "agreement",
        "summary",
        "report",
    )
    chosen = next((line for line in candidates if any(token in line.lower() for token in priority_patterns)), candidates[0])
    stem = re.sub(r"\s+", " ", chosen).strip(" .-_")
    stem = "".join(char for char in stem if char not in '<>:"/\\|?*').strip()
    if not stem:
        return fallback
    if len(stem) > FILENAME_MAX_STEM_LENGTH:
        stem = stem[:FILENAME_MAX_STEM_LENGTH].rstrip(" .-_")
    return _normalize_saved_filename(f"{stem}.pdf")


def _suggest_filename_from_ocr_text_result(full_text: str, fallback_filename: str) -> dict:
    suggested = suggest_filename_from_ocr_text(full_text, fallback_filename)
    fallback = _normalize_saved_filename(fallback_filename)
    if suggested == fallback:
        return {
            "filename": fallback,
            "source": "original",
            "reason": "heuristic_failed_kept_original",
            "details": {
                "heuristic_outcome": "heuristic_failed_kept_original",
                "heuristic_filename": fallback,
            },
        }
    return {
        "filename": suggested,
        "source": "ocr",
        "reason": "heuristic_succeeded",
        "details": {
            "heuristic_outcome": "heuristic_succeeded",
            "heuristic_filename": suggested,
        },
    }


def _sanitize_filename_stem(value: str) -> str:
    stem = re.sub(r"\.pdf\s*$", "", (value or "").strip(), flags=re.IGNORECASE)
    stem = re.sub(r"\s+", " ", stem)
    stem = stem.strip(" \"'`[](){}<>._-")
    stem = "".join(char for char in stem if char not in '<>:"/\\|?*').strip()
    if len(stem) > FILENAME_MAX_STEM_LENGTH:
        stem = stem[:FILENAME_MAX_STEM_LENGTH].rstrip(" .-_")
    return stem


def _looks_like_camera_filename(stem: str) -> bool:
    lowered = re.sub(r"[_\-\s]+", " ", (stem or "").strip().lower())
    if not lowered:
        return True
    if lowered in GENERIC_FILENAME_STEMS:
        return True
    return bool(
        re.fullmatch(
            r"(pxl|img|image|scan|scanned|photo|document|file|dsc|screenshot)[ _-]*[a-z0-9_-]*",
            lowered,
        )
    )


def _is_usable_filename_stem(stem: str, fallback_filename: str) -> bool:
    cleaned = _sanitize_filename_stem(stem)
    if len(cleaned) < 8:
        return False
    if sum(char.isalpha() for char in cleaned) < 5:
        return False
    if _looks_like_camera_filename(cleaned):
        return False

    fallback_stem = _sanitize_filename_stem(Path(_normalize_saved_filename(fallback_filename)).stem)
    if cleaned.lower() == fallback_stem.lower() and _looks_like_camera_filename(fallback_stem):
        return False
    return True


def suggest_filename_with_ai(full_text: str, fallback_filename: str) -> str | None:
    source_text = re.sub(r"\s+", " ", (full_text or "")).strip()
    if len(source_text) < MIN_AI_NAMING_TEXT_LENGTH:
        return None

    config = AssistantConfig.from_env()
    if not config.api_key:
        return None

    prompt = (
        "Suggest a concise saved PDF filename for this OCR'd legal or case-related document.\n"
        "Return only the filename stem, with no extension, no quotes, and no explanation.\n"
        "Use a clear human-readable document title based on the document's actual heading or type.\n"
        "Avoid generic names like Scan, Document, Image, Photo, Upload, or camera filenames.\n"
        "If the text is too weak to identify the document, return UNKNOWN.\n\n"
        f"Current fallback filename: {fallback_filename}\n\n"
        f"OCR text:\n{source_text[:6000]}"
    )

    try:
        suggestion = generate_text(
            prompt,
            api_key=config.api_key,
            model=config.model,
            instructions=(
                "You name OCR'd legal and case-related PDF documents. "
                "Reply with one filename stem only."
            ),
        )
    except OpenAIServiceError:
        return None

    cleaned = _sanitize_filename_stem(suggestion)
    if cleaned.upper() == "UNKNOWN":
        return None
    if not _is_usable_filename_stem(cleaned, fallback_filename):
        return None
    return _normalize_saved_filename(f"{cleaned}.pdf")


def suggest_filename_with_ai_from_images(page_image_paths: list[str], fallback_filename: str) -> str | None:
    if not page_image_paths:
        return None

    config = AssistantConfig.from_env()
    if not config.api_key:
        return None

    prompt = (
        "You are naming a saved PDF created from photographed document pages.\n"
        "Look at the page image and infer the most likely real document title or document type.\n"
        "Return only the filename stem, with no extension, no quotes, and no explanation.\n"
        "Use a clear human-readable title based on the visible heading or legal document type.\n"
        "Avoid generic names like Scan, Document, Image, Photo, Upload, or camera filenames.\n"
        "If the page is too unclear to identify, return UNKNOWN."
    )

    try:
        suggestion = generate_text_from_images(
            prompt,
            page_image_paths[:3],
            api_key=config.api_key,
            model=config.model,
            instructions=(
                "You name photographed legal and case-related PDF documents. "
                "Reply with one filename stem only."
            ),
        )
    except OpenAIServiceError:
        return None

    cleaned = _sanitize_filename_stem(suggestion)
    if cleaned.upper() == "UNKNOWN":
        return None
    if not _is_usable_filename_stem(cleaned, fallback_filename):
        return None
    return _normalize_saved_filename(f"{cleaned}.pdf")


def suggest_saved_document_naming(full_text: str, fallback_filename: str, page_image_paths: list[str] | None = None) -> dict:
    source_text = re.sub(r"\s+", " ", (full_text or "")).strip()
    ocr_text_length = len(source_text)
    fallback = _normalize_saved_filename(fallback_filename)
    usable_page_image_paths = [path for path in (page_image_paths or []) if path]

    if ocr_text_length < MIN_AI_NAMING_TEXT_LENGTH:
        config = AssistantConfig.from_env()
        heuristic = _suggest_filename_from_ocr_text_result(full_text, fallback)
        if not config.api_key:
            heuristic["details"]["ai_outcome"] = "ai_skipped_no_key"
            heuristic["details"]["ocr_text_length"] = ocr_text_length
            heuristic["details"]["fallback_filename"] = fallback
            heuristic["details"]["ai_error"] = "OPENAI_API_KEY missing"
            heuristic["details"]["final_source"] = heuristic["source"]
            heuristic["details"]["final_reason"] = heuristic["reason"]
            return heuristic
        if not usable_page_image_paths:
            heuristic["details"]["ai_outcome"] = "ai_skipped_short_text_no_images"
            heuristic["details"]["ocr_text_length"] = ocr_text_length
            heuristic["details"]["fallback_filename"] = fallback
            heuristic["details"]["ai_error"] = None
            heuristic["details"]["final_source"] = heuristic["source"]
            heuristic["details"]["final_reason"] = heuristic["reason"]
            return heuristic

        prompt = (
            "You are naming a saved PDF created from photographed document pages.\n"
            "Look at the page image and infer the most likely real document title or document type.\n"
            "Return only the filename stem, with no extension, no quotes, and no explanation.\n"
            "Use a clear human-readable title based on the visible heading or legal document type.\n"
            "Avoid generic names like Scan, Document, Image, Photo, Upload, or camera filenames.\n"
            "If the page is too unclear to identify, return UNKNOWN."
        )
        try:
            suggestion = generate_text_from_images(
                prompt,
                usable_page_image_paths[:3],
                api_key=config.api_key,
                model=config.model,
                instructions=(
                    "You name photographed legal and case-related PDF documents. "
                    "Reply with one filename stem only."
                ),
            )
        except OpenAIServiceError as exc:
            heuristic["details"]["ai_outcome"] = "ai_failed_request"
            heuristic["details"]["ocr_text_length"] = ocr_text_length
            heuristic["details"]["fallback_filename"] = fallback
            heuristic["details"]["ai_error"] = str(exc)
            heuristic["details"]["final_source"] = heuristic["source"]
            heuristic["details"]["final_reason"] = heuristic["reason"]
            return heuristic

        cleaned = _sanitize_filename_stem(suggestion)
        if cleaned.upper() == "UNKNOWN":
            heuristic["details"]["ai_outcome"] = "ai_vision_rejected_unknown"
            heuristic["details"]["ocr_text_length"] = ocr_text_length
            heuristic["details"]["fallback_filename"] = fallback
            heuristic["details"]["raw_ai_response"] = suggestion
            heuristic["details"]["ai_error"] = None
            heuristic["details"]["final_source"] = heuristic["source"]
            heuristic["details"]["final_reason"] = heuristic["reason"]
            return heuristic
        if not _is_usable_filename_stem(cleaned, fallback):
            heuristic["details"]["ai_outcome"] = "ai_vision_rejected_validation"
            heuristic["details"]["ocr_text_length"] = ocr_text_length
            heuristic["details"]["fallback_filename"] = fallback
            heuristic["details"]["raw_ai_stem"] = cleaned
            heuristic["details"]["raw_ai_response"] = suggestion
            heuristic["details"]["ai_error"] = None
            heuristic["details"]["final_source"] = heuristic["source"]
            heuristic["details"]["final_reason"] = heuristic["reason"]
            return heuristic

        return {
            "filename": _normalize_saved_filename(f"{cleaned}.pdf"),
            "source": "ai",
            "reason": "ai_vision_succeeded",
            "details": {
                "ai_outcome": "ai_vision_succeeded",
                "heuristic_outcome": heuristic["details"].get("heuristic_outcome"),
                "ocr_text_length": ocr_text_length,
                "fallback_filename": fallback,
                "raw_ai_stem": cleaned,
                "raw_ai_response": suggestion,
                "ai_error": None,
                "final_source": "ai",
                "final_reason": "ai_vision_succeeded",
            },
        }

    config = AssistantConfig.from_env()
    if not config.api_key:
        heuristic = _suggest_filename_from_ocr_text_result(full_text, fallback)
        heuristic["details"]["ai_outcome"] = "ai_skipped_no_key"
        heuristic["details"]["ocr_text_length"] = ocr_text_length
        heuristic["details"]["fallback_filename"] = fallback
        heuristic["details"]["ai_error"] = "OPENAI_API_KEY missing"
        heuristic["details"]["final_source"] = heuristic["source"]
        heuristic["details"]["final_reason"] = heuristic["reason"]
        return heuristic

    prompt = (
        "Suggest a concise saved PDF filename for this OCR'd legal or case-related document.\n"
        "Return only the filename stem, with no extension, no quotes, and no explanation.\n"
        "Use a clear human-readable document title based on the document's actual heading or type.\n"
        "Avoid generic names like Scan, Document, Image, Photo, Upload, or camera filenames.\n"
        "If the text is too weak to identify the document, return UNKNOWN.\n\n"
        f"Current fallback filename: {fallback}\n\n"
        f"OCR text:\n{source_text[:6000]}"
    )

    try:
        suggestion = generate_text(
            prompt,
            api_key=config.api_key,
            model=config.model,
            instructions=(
                "You name OCR'd legal and case-related PDF documents. "
                "Reply with one filename stem only."
            ),
        )
    except OpenAIServiceError as exc:
        heuristic = _suggest_filename_from_ocr_text_result(full_text, fallback)
        heuristic["details"]["ai_outcome"] = "ai_failed_request"
        heuristic["details"]["ocr_text_length"] = ocr_text_length
        heuristic["details"]["fallback_filename"] = fallback
        heuristic["details"]["ai_error"] = str(exc)
        heuristic["details"]["final_source"] = heuristic["source"]
        heuristic["details"]["final_reason"] = heuristic["reason"]
        return heuristic

    cleaned = _sanitize_filename_stem(suggestion)
    if cleaned.upper() == "UNKNOWN":
        heuristic = _suggest_filename_from_ocr_text_result(full_text, fallback)
        heuristic["details"]["ai_outcome"] = "ai_rejected_unknown"
        heuristic["details"]["ocr_text_length"] = ocr_text_length
        heuristic["details"]["fallback_filename"] = fallback
        heuristic["details"]["ai_error"] = None
        heuristic["details"]["raw_ai_response"] = suggestion
        heuristic["details"]["final_source"] = heuristic["source"]
        heuristic["details"]["final_reason"] = heuristic["reason"]
        return heuristic
    if not _is_usable_filename_stem(cleaned, fallback):
        heuristic = _suggest_filename_from_ocr_text_result(full_text, fallback)
        heuristic["details"]["ai_outcome"] = "ai_rejected_validation"
        heuristic["details"]["ocr_text_length"] = ocr_text_length
        heuristic["details"]["fallback_filename"] = fallback
        heuristic["details"]["raw_ai_stem"] = cleaned
        heuristic["details"]["raw_ai_response"] = suggestion
        heuristic["details"]["ai_error"] = None
        heuristic["details"]["final_source"] = heuristic["source"]
        heuristic["details"]["final_reason"] = heuristic["reason"]
        return heuristic

    return {
        "filename": _normalize_saved_filename(f"{cleaned}.pdf"),
        "source": "ai",
        "reason": "ai_succeeded",
        "details": {
            "ai_outcome": "ai_succeeded",
            "heuristic_outcome": None,
            "ocr_text_length": ocr_text_length,
            "fallback_filename": fallback,
            "raw_ai_stem": cleaned,
            "raw_ai_response": suggestion,
            "ai_error": None,
            "final_source": "ai",
            "final_reason": "ai_succeeded",
        },
    }


def suggest_saved_document_filename(full_text: str, fallback_filename: str) -> str:
    return suggest_saved_document_naming(full_text, fallback_filename)["filename"]


def rename_saved_document(job_id: str, filename: str) -> dict:
    result_path, payload = _load_result_payload(job_id)
    if not result_path or not payload:
        return {"status": "failed", "errors": ["Saved OCR document not found."]}

    current_original_path = payload.get("original_pdf_path")
    original_path = Path(current_original_path) if current_original_path else None
    if not original_path or not original_path.is_file():
        return {"status": "failed", "errors": ["Original saved PDF file is missing."]}

    # The physical file's extension must always reflect its real format —
    # renaming only changes the label. Without this, typing a new name
    # ending in ".docx" (say, out of habit for a converted Word doc) would
    # silently mismatch the stored .pdf's actual extension and break the
    # viewer's content-type detection (same bug class fixed in
    # list_recent_scans for the docx-conversion case).
    real_suffix = original_path.suffix
    new_filename = _normalize_saved_filename(filename, default_extension=real_suffix or ".pdf")
    if Path(new_filename).suffix.lower() != real_suffix.lower():
        new_filename = f"{Path(new_filename).stem}{real_suffix}"

    target_path = original_path.with_name(new_filename)
    if target_path != original_path:
        if target_path.exists():
            target_path.unlink()
        original_path.rename(target_path)

    current_text_path = payload.get("extracted_text_path")
    text_path = Path(current_text_path) if current_text_path else None
    if text_path and text_path.is_file():
        target_text_path = text_path.with_name(f"{Path(new_filename).stem}.txt")
        if target_text_path != text_path:
            if target_text_path.exists():
                target_text_path.unlink()
            text_path.rename(target_text_path)
        payload["extracted_text_path"] = str(target_text_path)

    payload["original_filename"] = new_filename
    payload["original_pdf_path"] = str(target_path)
    # matchFilename is deliberately untouched — this is the whole point:
    # document-registry matching keeps working against the frozen original
    # name no matter what this document gets renamed to (see matcher.py).
    _save_result_payload(job_id, payload)
    return payload


def scan_upload(file_bytes: bytes, filename: str) -> dict:
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        return scan_pdf(file_bytes, filename)
    if suffix in SUPPORTED_IMAGE_EXTENSIONS:
        return scan_image(file_bytes, filename)
    return {
        "job_id": uuid.uuid4().hex,
        "status": "failed",
        "original_filename": filename,
        "original_pdf_path": None,
        "page_count": 0,
        "page_image_paths": [],
        "full_text": "",
        "pages": [],
        "extracted_text_path": None,
        "json_result_path": None,
        "warnings": [],
        "errors": [f"Unsupported file type '{suffix or 'unknown'}'. Supported: PDF, PNG, JPG, JPEG, WEBP."],
    }


def scan_image(file_bytes: bytes, filename: str) -> dict:
    storage_root = get_ocr_storage_root()
    storage_root.mkdir(parents=True, exist_ok=True)
    config = replace(OCRServiceConfig.from_env(), storage_root=storage_root)
    job_id = uuid.uuid4().hex
    dirs = storage.create_job_dirs(config.storage_root, job_id)
    original_path = storage.save_original_pdf(file_bytes, filename, dirs.original_dir)

    try:
        page_images = preprocessing.image_bytes_to_page_images(file_bytes)
    except Exception as exc:  # noqa: BLE001
        return _save_failed_result(job_id, filename, original_path, dirs, f"Failed to read image: {exc}")

    pages: list[PageResult] = []
    page_image_paths: list[str] = []
    errors: list[str] = []
    warnings: list[str] = []
    failed_page_count = 0

    for index, page_image in enumerate(page_images, start=1):
        image_path = storage.save_page_png(page_image, index, dirs.pages_dir)
        page_image_paths.append(image_path)
        try:
            png_bytes = preprocessing.image_to_png_bytes(page_image)
            raw_response = provider.call_eden_ocr(
                png_bytes,
                api_key=config.require_api_key(),
                provider=config.provider,
                language=config.language,
                timeout=config.request_timeout_seconds,
                max_retries=config.max_retries,
            )
            text, page_warnings = provider.extract_text_from_response(raw_response, provider=config.provider)
            pages.append(PageResult(page_number=index, text=text, image_path=image_path))
            warnings.extend(f"Page {index}: {w}" for w in page_warnings)
        except Exception as exc:  # noqa: BLE001
            failed_page_count += 1
            errors.append(f"Page {index}: OCR failed: {exc}")
            pages.append(PageResult(page_number=index, text=f"[OCR FAILED: {exc}]", image_path=image_path))

    full_text = normalize.combine_full_text(pages)
    status = normalize.determine_status(len(pages), failed_page_count)
    result = OCRResult(
        job_id=job_id,
        status=status,
        original_filename=filename,
        original_pdf_path=original_path,
        page_count=len(pages),
        page_image_paths=page_image_paths,
        full_text=full_text,
        pages=pages,
        extracted_text_path=str(storage.extracted_text_path(dirs.results_dir)),
        json_result_path=str(storage.json_result_path(dirs.results_dir)),
        warnings=warnings,
        errors=errors,
    )
    storage.save_extracted_text(full_text, dirs.results_dir)
    storage.save_json_result(result.to_dict(), dirs.results_dir)
    return result.to_dict()

def _save_failed_result(job_id: str, filename: str, original_path: str | None, dirs, message: str) -> dict:
    result = OCRResult(
        job_id=job_id,
        status="failed",
        original_filename=filename,
        original_pdf_path=original_path,
        page_count=0,
        page_image_paths=[],
        full_text="",
        pages=[],
        extracted_text_path=str(storage.extracted_text_path(dirs.results_dir)),
        json_result_path=str(storage.json_result_path(dirs.results_dir)),
        warnings=[],
        errors=[message],
    )
    storage.save_extracted_text("", dirs.results_dir)
    storage.save_json_result(result.to_dict(), dirs.results_dir)
    return result.to_dict()


def list_recent_scans(limit: int | None = 5) -> list[dict]:
    """Reads back the job folders ocr_document() already wrote under DLYTE's
    storage root (result.json per job) — no OCR logic here, just a listing."""
    jobs_dir = get_ocr_storage_root() / "jobs"
    if not jobs_dir.is_dir():
        return []

    entries = []
    for job_dir in jobs_dir.iterdir():
        result_path = job_dir / "results" / "result.json"
        if not result_path.is_file():
            continue
        try:
            data = json.loads(result_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        filename = data.get("original_filename", "unknown")
        # Gate on the *stored* file's extension, not the display filename's:
        # a converted .doc/.docx job is relabeled back to its original name
        # for document-registry matching (see scan_runner.run_queued_scans),
        # but the file actually sitting in original/ is a real .pdf and
        # should still show up here as an openable scan.
        stored_path = data.get("original_pdf_path")
        stored_suffix = Path(stored_path).suffix.lower() if stored_path else Path(filename).suffix.lower()
        if stored_suffix not in RECENT_SCAN_EXTENSIONS:
            continue
        entries.append(
            {
                "job_id": data.get("job_id", job_dir.name),
                "filename": filename,
                "page_count": data.get("page_count", 0),
                "status": data.get("status", "unknown"),
                "scanned_at": result_path.stat().st_mtime,
            }
        )

    entries.sort(key=lambda e: e["scanned_at"], reverse=True)
    if limit is None:
        return entries
    return entries[:limit]


def get_original_file_path(job_id: str) -> Path | None:
    """Returns the original uploaded file path for a known intake job."""
    job_dir = get_ocr_storage_root() / "jobs" / job_id / "original"
    if not job_dir.is_dir():
        return None

    for path in job_dir.iterdir():
        if path.is_file():
            return path
    return None
