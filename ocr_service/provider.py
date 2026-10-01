import time

import requests

from .errors import OCRServiceError

# Same endpoint used by the existing ocr_scanned_pdf.py.
EDEN_API_URL = "https://api.edenai.run/v2/ocr/ocr"


def call_eden_ocr(
    image_bytes: bytes,
    *,
    api_key: str,
    provider: str = "google",
    language: str = "en",
    timeout: int = 120,
    max_retries: int = 2,
) -> dict:
    """
    Send a single page image to Eden AI's OCR endpoint and return the raw
    provider response as a dict.

    Same provider/API/config approach as the existing script's run_ocr():
    Eden AI, Bearer token auth, multipart file upload. Adapted to accept
    in-memory bytes (so callers never need to write a temp file to disk)
    and to retry transient failures, which the original script did not do.
    """
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            response = requests.post(
                EDEN_API_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": ("page.png", image_bytes, "image/png")},
                data={"providers": provider, "language": language},
                timeout=timeout,
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            last_error = exc
            if attempt < max_retries:
                time.sleep(min(2 ** attempt, 8))
                continue

    raise OCRServiceError(f"OCR provider request failed after retries: {last_error}")


def extract_text_from_response(ocr_result: dict, provider: str = "google") -> tuple[str, list[str]]:
    """
    Pull the extracted text out of a raw Eden AI response.

    Same fallback behavior as the existing script's extract_text(): if the
    expected shape isn't there, fall back to a stringified dump rather than
    raising, but this version also reports it as a warning so callers know
    the fallback path was taken.
    """
    try:
        return ocr_result[provider]["text"], []
    except (KeyError, TypeError):
        warning = f"Unexpected OCR response shape from provider '{provider}'; returned raw response as text."
        return str(ocr_result), [warning]
