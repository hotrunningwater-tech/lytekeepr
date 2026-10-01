import io

from pdf2image import convert_from_bytes
from PIL import Image


def pdf_bytes_to_page_images(pdf_data: bytes, dpi: int = 300) -> list[Image.Image]:
    """
    Rasterize a PDF (as bytes) into one Pillow image per page.

    Same approach as the existing ocr_scanned_pdf.py (pdf2image at 300 DPI),
    adapted to work from in-memory bytes instead of a file path so nothing
    has to be written to disk before OCR.
    """
    return convert_from_bytes(pdf_data, dpi=dpi)


def image_bytes_to_page_images(image_data: bytes) -> list[Image.Image]:
    """
    Wrap a single image file's bytes as a one-page 'document' so it can
    flow through the same page-by-page pipeline as a PDF.
    """
    image = Image.open(io.BytesIO(image_data))
    image.load()
    return [image]


def image_to_png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue()
