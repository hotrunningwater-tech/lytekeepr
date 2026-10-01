from dataclasses import dataclass, field


@dataclass
class PageResult:
    page_number: int
    text: str
    image_path: str | None = None

    def to_dict(self) -> dict:
        return {
            "page_number": self.page_number,
            "text": self.text,
            "image_path": self.image_path,
        }


@dataclass
class OCRResult:
    job_id: str
    status: str  # "success" | "partial" | "failed"
    original_filename: str
    original_pdf_path: str | None
    page_count: int
    page_image_paths: list[str]
    full_text: str
    pages: list[PageResult]
    extracted_text_path: str | None
    json_result_path: str | None
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "original_filename": self.original_filename,
            "original_pdf_path": self.original_pdf_path,
            "page_count": self.page_count,
            "page_image_paths": self.page_image_paths,
            "full_text": self.full_text,
            "pages": [p.to_dict() for p in self.pages],
            "extracted_text_path": self.extracted_text_path,
            "json_result_path": self.json_result_path,
            "warnings": self.warnings,
            "errors": self.errors,
        }
