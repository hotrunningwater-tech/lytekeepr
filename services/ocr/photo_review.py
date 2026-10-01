"""Photographed-page review and PDF finalization for image uploads."""

from __future__ import annotations

import io
import json
import logging
import shutil
import uuid
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from doc_registry.photo_review_state import set_generated_pdf
from services.ocr.adapter import get_ocr_storage_root, scan_pdf, suggest_saved_document_naming, _save_result_payload

logger = logging.getLogger(__name__)
SUPPORTED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


def _photo_review_root() -> Path:
    return get_ocr_storage_root() / "photo-review"


def _manifest_path(job_id: str) -> Path:
    return _photo_review_root() / job_id / "meta" / "manifest.json"


def _photo_review_dirs(job_id: str) -> dict[str, Path]:
    job_dir = _photo_review_root() / job_id
    dirs = {
        "job": job_dir,
        "original": job_dir / "original",
        "preview": job_dir / "preview",
        "meta": job_dir / "meta",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs


def _load_manifest(job_id: str) -> dict | None:
    path = _manifest_path(job_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _save_manifest(job_id: str, manifest: dict) -> None:
    _manifest_path(job_id).write_text(json.dumps(manifest, indent=2))


def _asset_url(job_id: str, asset_kind: str, filename: str) -> str:
    return f"/api/photo-review/{job_id}/assets/{asset_kind}/{filename}"


def _image_from_bytes(file_bytes: bytes) -> np.ndarray:
    array = np.frombuffer(file_bytes, dtype=np.uint8)
    image = cv2.imdecode(array, cv2.IMREAD_COLOR)
    if image is not None:
        return image

    # Some phone-generated JPEGs decode poorly through OpenCV alone.
    # Fall back to Pillow so photographed pages from mobile devices still
    # enter the review flow reliably.
    try:
        with Image.open(io.BytesIO(file_bytes)) as pil_image:
            rgb_image = pil_image.convert("RGB")
            image = cv2.cvtColor(np.array(rgb_image), cv2.COLOR_RGB2BGR)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Could not read image bytes: {exc}") from exc

    if image is None:
        raise ValueError("Could not read image bytes.")
    return image


def _order_points(points: np.ndarray) -> np.ndarray:
    rect = np.zeros((4, 2), dtype=np.float32)
    sums = points.sum(axis=1)
    rect[0] = points[np.argmin(sums)]
    rect[2] = points[np.argmax(sums)]

    diffs = np.diff(points, axis=1)
    rect[1] = points[np.argmin(diffs)]
    rect[3] = points[np.argmax(diffs)]
    return rect


def _detect_document_corners(image: np.ndarray) -> tuple[np.ndarray, bool]:
    height, width = image.shape[:2]
    scale = 1.0
    max_dim = max(height, width)
    working = image
    if max_dim > 1600:
        scale = 1600.0 / max_dim
        working = cv2.resize(image, (int(width * scale), int(height * scale)), interpolation=cv2.INTER_AREA)

    gray = cv2.cvtColor(working, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edged = cv2.Canny(blurred, 75, 200)
    kernel = np.ones((5, 5), dtype=np.uint8)
    edged = cv2.dilate(edged, kernel, iterations=2)
    edged = cv2.erode(edged, kernel, iterations=1)

    contours, _ = cv2.findContours(edged, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    contours = sorted(contours, key=cv2.contourArea, reverse=True)

    for contour in contours[:10]:
        perimeter = cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, 0.02 * perimeter, True)
        if len(approx) != 4:
            continue
        area = cv2.contourArea(approx)
        if area < working.shape[0] * working.shape[1] * 0.15:
            continue
        points = approx.reshape(4, 2).astype(np.float32) / scale
        return _order_points(points), True

    # Keep fallback corners slightly inset so the crop polygon and handles stay
    # visible/editable even when automatic page detection fails.
    inset_x = min(max(width * 0.03, 18.0), max(width / 2 - 2, 1))
    inset_y = min(max(height * 0.03, 18.0), max(height / 2 - 2, 1))
    fallback = np.array(
        [
            [inset_x, inset_y],
            [width - 1 - inset_x, inset_y],
            [width - 1 - inset_x, height - 1 - inset_y],
            [inset_x, height - 1 - inset_y],
        ],
        dtype=np.float32,
    )
    return fallback, False


def _corners_to_normalized(corners: np.ndarray, width: int, height: int) -> list[dict]:
    return [{"x": float(point[0] / width), "y": float(point[1] / height)} for point in corners]


def _normalize_corner_payload(corners: list[dict]) -> list[dict]:
    normalized = []
    for point in corners:
        x = min(1.0, max(0.0, float(point["x"])))
        y = min(1.0, max(0.0, float(point["y"])))
        normalized.append({"x": x, "y": y})
    if len(normalized) != 4:
        raise ValueError("Exactly four crop corners are required.")
    return normalized


def _normalized_to_points(corners: list[dict], width: int, height: int) -> np.ndarray:
    points = []
    for point in _normalize_corner_payload(corners):
        points.append([point["x"] * width, point["y"] * height])
    return _order_points(np.array(points, dtype=np.float32))


def _rotate_image(image: np.ndarray, rotation_turns: int) -> np.ndarray:
    turns = rotation_turns % 4
    if turns == 1:
        return cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
    if turns == 2:
        return cv2.rotate(image, cv2.ROTATE_180)
    if turns == 3:
        return cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return image


def _warp_document(image: np.ndarray, corners: list[dict], rotation_turns: int) -> np.ndarray:
    height, width = image.shape[:2]
    rect = _normalized_to_points(corners, width, height)

    width_top = np.linalg.norm(rect[1] - rect[0])
    width_bottom = np.linalg.norm(rect[2] - rect[3])
    max_width = max(int(width_top), int(width_bottom), 1)

    height_right = np.linalg.norm(rect[2] - rect[1])
    height_left = np.linalg.norm(rect[3] - rect[0])
    max_height = max(int(height_right), int(height_left), 1)

    destination = np.array(
        [[0, 0], [max_width - 1, 0], [max_width - 1, max_height - 1], [0, max_height - 1]],
        dtype=np.float32,
    )
    matrix = cv2.getPerspectiveTransform(rect, destination)
    warped = cv2.warpPerspective(image, matrix, (max_width, max_height))
    return _rotate_image(warped, rotation_turns)


def _save_preview(job_id: str, page: dict, image: np.ndarray) -> None:
    preview_path = PHOTO_REVIEW_ROOT / job_id / "preview" / page["previewAsset"]
    preview = image
    preview_height, preview_width = preview.shape[:2]
    max_dim = max(preview_width, preview_height)
    if max_dim > 1200:
        scale = 1200.0 / max_dim
        preview = cv2.resize(
            preview,
            (max(1, int(preview_width * scale)), max(1, int(preview_height * scale))),
            interpolation=cv2.INTER_AREA,
        )
    cv2.imwrite(str(preview_path), preview)


def _page_response(job_id: str, page: dict) -> dict:
    return {
        "id": page["id"],
        "filename": page["filename"],
        "originalWidth": page["originalWidth"],
        "originalHeight": page["originalHeight"],
        "corners": page["corners"],
        "rotationTurns": page["rotationTurns"],
        "detected": page["detected"],
        "deleted": page["deleted"],
        "originalUrl": _asset_url(job_id, "original", page["originalAsset"]),
        "previewUrl": _asset_url(job_id, "preview", page["previewAsset"]),
    }


def _build_review_payload(job_id: str, manifest: dict) -> dict:
    return {
        "job_id": job_id,
        "pages": [_page_response(job_id, page) for page in manifest["pages"]],
        "outputFilename": manifest["outputFilename"],
    }


def _find_page(manifest: dict, page_id: str) -> dict | None:
    for page in manifest["pages"]:
        if page["id"] == page_id:
            return page
    return None


def _load_original_image(job_id: str, page: dict) -> np.ndarray:
    path = PHOTO_REVIEW_ROOT / job_id / "original" / page["originalAsset"]
    return _image_from_bytes(path.read_bytes())


def start_photo_review(files: list[tuple[str, bytes]], source: dict | None = None) -> dict:
    if not files:
        return {"status": "failed", "errors": ["No image files provided."]}

    job_id = uuid.uuid4().hex
    dirs = _photo_review_dirs(job_id)
    pages = []

    for index, (filename, file_bytes) in enumerate(files, start=1):
        suffix = Path(filename).suffix.lower()
        if suffix not in SUPPORTED_IMAGE_EXTENSIONS:
            return {"status": "failed", "errors": [f"Unsupported image type '{suffix or 'unknown'}'."]}

        image = _image_from_bytes(file_bytes)
        height, width = image.shape[:2]
        corners, detected = _detect_document_corners(image)
        original_asset = f"page-{index:03d}{suffix}"
        preview_asset = f"page-{index:03d}.png"
        (dirs["original"] / original_asset).write_bytes(file_bytes)

        page = {
            "id": uuid.uuid4().hex,
            "index": index,
            "filename": filename,
            "originalAsset": original_asset,
            "previewAsset": preview_asset,
            "originalWidth": width,
            "originalHeight": height,
            "corners": _corners_to_normalized(corners, width, height),
            "rotationTurns": 0,
            "detected": detected,
            "deleted": False,
        }
        _save_preview(job_id, page, _warp_document(image, page["corners"], 0))
        pages.append(page)

    stem = Path(files[0][0]).stem if len(files) == 1 else "scanned-pages"
    manifest = {"jobId": job_id, "outputFilename": f"{stem}.pdf", "pages": pages, "source": source or None}
    _save_manifest(job_id, manifest)
    return {"status": "ready", "review": _build_review_payload(job_id, manifest)}


def regenerate_preview(job_id: str, page_id: str, corners: list[dict], rotation_turns: int) -> dict:
    manifest = _load_manifest(job_id)
    if not manifest:
        return {"status": "not_found", "message": "Photo review job not found."}
    page = _find_page(manifest, page_id)
    if not page:
        return {"status": "not_found", "message": "Photo review page not found."}

    page["corners"] = _normalize_corner_payload(corners)
    page["rotationTurns"] = int(rotation_turns) % 4
    image = _load_original_image(job_id, page)
    _save_preview(job_id, page, _warp_document(image, page["corners"], page["rotationTurns"]))
    _save_manifest(job_id, manifest)
    return {"status": "updated", "page": _page_response(job_id, page)}


def _build_processed_pil(job_id: str, page: dict, payload: dict) -> Image.Image:
    corners = _normalize_corner_payload(payload.get("corners") or page["corners"])
    rotation_turns = int(payload.get("rotationTurns", page.get("rotationTurns", 0))) % 4
    image = _load_original_image(job_id, page)
    processed = _warp_document(image, corners, rotation_turns)
    rgb = cv2.cvtColor(processed, cv2.COLOR_BGR2RGB)
    return Image.fromarray(rgb)


def _copy_originals_to_ocr_job(review_job_id: str, ocr_job_id: str) -> None:
    source_dir = PHOTO_REVIEW_ROOT / review_job_id / "original"
    target_dir = get_ocr_storage_root() / "jobs" / ocr_job_id / "photo-source"
    target_dir.mkdir(parents=True, exist_ok=True)
    for path in source_dir.iterdir():
        if path.is_file():
            shutil.copy2(path, target_dir / path.name)


def _normalize_output_filename(value: str | None, fallback: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return fallback
    cleaned = "".join(char for char in raw if char not in '<>:"/\\|?*').strip().strip(".")
    if not cleaned:
        return fallback
    if not cleaned.lower().endswith(".pdf"):
        cleaned = f"{cleaned}.pdf"
    return cleaned


def finalize_photo_review(job_id: str, pages_payload: list[dict], output_filename: str | None = None) -> dict:
    manifest = _load_manifest(job_id)
    if not manifest:
        return {"status": "failed", "errors": ["Photo review job not found."]}
    if not isinstance(pages_payload, list):
        return {"status": "failed", "errors": ["Finalize request must include a pages list."]}

    page_lookup = {page["id"]: page for page in manifest["pages"]}
    final_pages = []
    used_ids = set()
    for payload in pages_payload:
        page_id = payload.get("id")
        if not isinstance(page_id, str) or page_id not in page_lookup or page_id in used_ids:
            continue
        used_ids.add(page_id)
        if payload.get("deleted"):
            continue
        final_pages.append(_build_processed_pil(job_id, page_lookup[page_id], payload))

    if not final_pages:
        return {"status": "failed", "errors": ["At least one page must remain before finalizing."]}

    buffer = io.BytesIO()
    first, rest = final_pages[0], final_pages[1:]
    first.convert("RGB").save(
        buffer,
        format="PDF",
        save_all=True,
        append_images=[image.convert("RGB") for image in rest],
    )
    final_filename = _normalize_output_filename(output_filename, manifest["outputFilename"])
    manifest["outputFilename"] = final_filename
    _save_manifest(job_id, manifest)
    result = scan_pdf(buffer.getvalue(), final_filename)
    if result.get("status") == "failed":
        return result

    source = manifest.get("source") or {}
    if source.get("kind") == "email":
        for attachment in source.get("attachments", []) or []:
            message_id = attachment.get("messageId")
            attachment_id = attachment.get("attachmentId")
            if message_id and attachment_id:
                set_generated_pdf(message_id, attachment_id, job_id, result["job_id"])
    _copy_originals_to_ocr_job(job_id, result["job_id"])
    result["source_images_preserved"] = True
    result["source_review_job_id"] = job_id
    result["final_filename"] = final_filename
    naming = suggest_saved_document_naming(
        result.get("full_text", ""),
        final_filename,
        page_image_paths=result.get("page_image_paths") or [],
    )
    result["suggested_filename"] = naming["filename"]
    result["naming_source"] = naming["source"]
    result["naming_reason"] = naming["reason"]
    result["naming_details"] = naming["details"]
    result["naming_debug"] = {
        "final_filename": final_filename,
        "suggested_filename": naming["filename"],
        "source": naming["source"],
        "reason": naming["reason"],
        "details": naming["details"],
    }
    logger.warning("photo_review_naming_debug job_id=%s debug=%s", result["job_id"], result["naming_debug"])
    result["ocr_completed"] = result.get("status") in {"success", "partial"}
    _save_result_payload(result["job_id"], result)
    return result


def get_asset_path(job_id: str, asset_kind: str, filename: str) -> Path | None:
    if asset_kind not in {"original", "preview"}:
        return None
    path = PHOTO_REVIEW_ROOT / job_id / asset_kind / filename
    if not path.is_file():
        return None
    return path
