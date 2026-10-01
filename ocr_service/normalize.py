from .models import PageResult


def combine_full_text(pages: list[PageResult]) -> str:
    return "\n\n".join(f"--- Page {p.page_number} ---\n{p.text}" for p in pages)


def determine_status(page_count: int, failed_page_count: int) -> str:
    if page_count == 0 or failed_page_count == page_count:
        return "failed"
    if failed_page_count > 0:
        return "partial"
    return "success"
