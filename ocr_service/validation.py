from dataclasses import dataclass
from pathlib import Path

from .config import SUPPORTED_EXTENSIONS
from .errors import FileValidationError, UnsupportedFileTypeError


@dataclass
class ResolvedInput:
    """Normalized view of whatever was handed to the service."""

    filename: str
    data: bytes


def resolve_input(file, filename: str | None = None) -> ResolvedInput:
    """
    Accepts any of:
      - a str or Path pointing to an existing file on disk
      - a file-like object with .read() (e.g. an upload handler's file object),
        optionally exposing .filename or .name
      - raw bytes, in which case `filename` must be supplied

    Returns a ResolvedInput with the raw bytes already loaded into memory,
    so downstream steps never need to know where the data came from.
    """
    if isinstance(file, (str, Path)):
        path = Path(file)
        if not path.is_file():
            raise FileValidationError(f"File not found: {path}")
        resolved_name = filename or path.name
        data = path.read_bytes()

    elif hasattr(file, "read"):
        resolved_name = filename or getattr(file, "filename", None) or getattr(file, "name", None)
        if not resolved_name:
            raise FileValidationError(
                "Could not determine a filename from the given file object; "
                "pass filename= explicitly."
            )
        resolved_name = Path(resolved_name).name
        data = file.read()
        if isinstance(data, str):
            data = data.encode("utf-8")

    elif isinstance(file, (bytes, bytearray)):
        if not filename:
            raise FileValidationError("filename= is required when passing raw bytes.")
        resolved_name = filename
        data = bytes(file)

    else:
        raise FileValidationError(f"Unsupported input type for OCR: {type(file)!r}")

    if not data:
        raise FileValidationError(f"'{resolved_name}' is empty.")

    suffix = Path(resolved_name).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise UnsupportedFileTypeError(
            f"'{resolved_name}' has type '{suffix or 'unknown'}'. "
            "Only PDF files are currently supported."
        )

    return ResolvedInput(filename=resolved_name, data=data)
