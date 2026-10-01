"""
.doc / .docx -> PDF conversion, so Word documents can go through the exact
same OCR pipeline as everything else (page-level text with coordinates,
one consistent preview/viewer path) instead of a separate text-only
extraction path.

Shells out to LibreOffice headless (`soffice --headless --convert-to pdf`)
— the standard way to get faithful, print-accurate DOCX->PDF conversion
without a Microsoft Word license. Requires LibreOffice installed on the
host machine; this module does not install it.
"""

import shutil
import subprocess
import tempfile
from pathlib import Path

WORD_EXTENSIONS = {".doc", ".docx"}

# Common install locations across platforms; `shutil.which` covers PATH,
# this covers the macOS .app bundle case where the binary usually isn't
# symlinked onto PATH by the LibreOffice installer.
_MAC_SOFFICE_PATH = "/Applications/LibreOffice.app/Contents/MacOS/soffice"


class DocConversionError(Exception):
    """Raised when a .doc/.docx cannot be converted to PDF."""


def is_word_document(filename: str) -> bool:
    return Path(filename or "").suffix.lower() in WORD_EXTENSIONS


def _find_soffice() -> str:
    found = shutil.which("soffice")
    if found:
        return found
    if Path(_MAC_SOFFICE_PATH).is_file():
        return _MAC_SOFFICE_PATH
    raise DocConversionError(
        "LibreOffice (soffice) is not installed — required to convert .doc/.docx to PDF. "
        "Install it (e.g. `brew install --cask libreoffice` on macOS) and try again."
    )


def convert_to_pdf_bytes(file_bytes: bytes, filename: str, *, timeout: int = 60) -> bytes:
    """Converts a .doc/.docx file's bytes to PDF bytes. Raises
    DocConversionError on any failure (missing LibreOffice, corrupt file,
    conversion timeout, etc.) — callers should catch this and treat it the
    same as any other per-item scan failure."""
    soffice = _find_soffice()
    suffix = Path(filename or "").suffix or ".docx"

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_dir_path = Path(tmp_dir)
        input_path = tmp_dir_path / f"input{suffix}"
        input_path.write_bytes(file_bytes)

        try:
            subprocess.run(
                [
                    soffice,
                    "--headless",
                    "--norestore",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    str(tmp_dir_path),
                    str(input_path),
                ],
                check=True,
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise DocConversionError(f"Conversion of '{filename}' timed out after {timeout}s.") from exc
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or b"").decode("utf-8", errors="replace").strip()
            raise DocConversionError(f"LibreOffice failed to convert '{filename}': {stderr or 'unknown error'}") from exc

        output_path = tmp_dir_path / "input.pdf"
        if not output_path.is_file():
            raise DocConversionError(f"Conversion of '{filename}' produced no PDF output.")
        return output_path.read_bytes()
