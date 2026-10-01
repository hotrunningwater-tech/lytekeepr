import uuid

from . import normalize, preprocessing, provider, storage, validation
from .config import OCRServiceConfig
from .errors import OCRServiceError
from .models import OCRResult, PageResult


def _failed_result(job_id: str, filename: str, message: str) -> OCRResult:
    return OCRResult(
        job_id=job_id,
        status="failed",
        original_filename=filename,
        original_pdf_path=None,
        page_count=0,
        page_image_paths=[],
        full_text="",
        pages=[],
        extracted_text_path=None,
        json_result_path=None,
        errors=[message],
    )


def ocr_document(
    file,
    *,
    filename: str | None = None,
    config: OCRServiceConfig | None = None,
) -> OCRResult:
    """
    The one public entry point for the OCR service.

    Workflow: PDF in -> split into pages -> each page rendered to PNG ->
    only the PNGs are sent to the OCR provider -> text combined in page
    order -> original PDF, page PNGs, extracted text, and the structured
    JSON result are all saved under a per-job folder -> structured
    OCRResult returned.

    Input (any one of): a path (str/Path), a file-like object (has
    .read(), optionally .filename/.name), or raw bytes (filename=
    required in that case). PDF only for this phase.

    Never raises for expected failure modes (bad file, non-PDF,
    missing API key, a page's OCR call failing) — always returns an
    OCRResult with `.status` set, so a UI can call this without
    wrapping every call in try/except.
    """
    active_config = config or OCRServiceConfig.from_env()
    job_id = uuid.uuid4().hex

    try:
        resolved = validation.resolve_input(file, filename=filename)
    except OCRServiceError as exc:
        return _failed_result(job_id, filename or "<unknown>", str(exc))

    try:
        api_key = active_config.require_api_key()
    except OCRServiceError as exc:
        return _failed_result(job_id, resolved.filename, str(exc))

    dirs = storage.create_job_dirs(active_config.storage_root, job_id)
    original_pdf_path = storage.save_original_pdf(resolved.data, resolved.filename, dirs.original_dir)

    try:
        page_images = preprocessing.pdf_bytes_to_page_images(resolved.data, dpi=active_config.pdf_dpi)
    except Exception as exc:  # noqa: BLE001 - surfaced as a structured result, not a crash
        result = _failed_result(job_id, resolved.filename, f"Failed to split PDF into pages: {exc}")
        result.original_pdf_path = original_pdf_path
        result.extracted_text_path = str(storage.extracted_text_path(dirs.results_dir))
        result.json_result_path = str(storage.json_result_path(dirs.results_dir))
        storage.save_extracted_text("", dirs.results_dir)
        storage.save_json_result(result.to_dict(), dirs.results_dir)
        return result

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
                api_key=api_key,
                provider=active_config.provider,
                language=active_config.language,
                timeout=active_config.request_timeout_seconds,
                max_retries=active_config.max_retries,
            )
            text, page_warnings = provider.extract_text_from_response(
                raw_response, provider=active_config.provider
            )
            pages.append(PageResult(page_number=index, text=text, image_path=image_path))
            warnings.extend(f"Page {index}: {w}" for w in page_warnings)
        except Exception as exc:  # noqa: BLE001 - one failed page shouldn't stop the rest
            failed_page_count += 1
            error_message = f"Page {index}: OCR failed: {exc}"
            errors.append(error_message)
            pages.append(
                PageResult(page_number=index, text=f"[OCR FAILED: {exc}]", image_path=image_path)
            )

    full_text = normalize.combine_full_text(pages)
    status = normalize.determine_status(len(pages), failed_page_count)

    result = OCRResult(
        job_id=job_id,
        status=status,
        original_filename=resolved.filename,
        original_pdf_path=original_pdf_path,
        page_count=len(pages),
        page_image_paths=page_image_paths,
        full_text=full_text,
        pages=pages,
        extracted_text_path=None,
        json_result_path=None,
        warnings=warnings,
        errors=errors,
    )

    result.extracted_text_path = str(storage.extracted_text_path(dirs.results_dir))
    result.json_result_path = str(storage.json_result_path(dirs.results_dir))
    storage.save_extracted_text(full_text, dirs.results_dir)
    storage.save_json_result(result.to_dict(), dirs.results_dir)

    return result
