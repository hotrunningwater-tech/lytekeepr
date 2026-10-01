class OCRServiceError(Exception):
    """Base class for all ocr_service exceptions."""


class ConfigurationError(OCRServiceError):
    """Raised when the service is misconfigured (e.g. missing API key)."""


class FileValidationError(OCRServiceError):
    """Raised when the input file cannot be read or does not exist."""


class UnsupportedFileTypeError(OCRServiceError):
    """Raised when the input file extension is not supported."""
