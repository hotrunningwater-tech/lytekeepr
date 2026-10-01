import json
from dataclasses import dataclass
from pathlib import Path

from PIL import Image


@dataclass
class JobDirs:
    job_dir: Path
    original_dir: Path
    pages_dir: Path
    results_dir: Path


def create_job_dirs(storage_root: Path, job_id: str) -> JobDirs:
    job_dir = storage_root / "jobs" / job_id
    dirs = JobDirs(
        job_dir=job_dir,
        original_dir=job_dir / "original",
        pages_dir=job_dir / "pages",
        results_dir=job_dir / "results",
    )
    for d in (dirs.original_dir, dirs.pages_dir, dirs.results_dir):
        d.mkdir(parents=True, exist_ok=True)
    return dirs


def save_original_pdf(data: bytes, filename: str, original_dir: Path) -> str:
    path = original_dir / filename
    path.write_bytes(data)
    return str(path)


def save_page_png(image: Image.Image, page_number: int, pages_dir: Path) -> str:
    path = pages_dir / f"page-{page_number:03d}.png"
    image.convert("RGB").save(path, "PNG")
    return str(path)


def extracted_text_path(results_dir: Path) -> Path:
    return results_dir / "extracted-text.txt"


def json_result_path(results_dir: Path) -> Path:
    return results_dir / "result.json"


def save_extracted_text(text: str, results_dir: Path) -> str:
    path = extracted_text_path(results_dir)
    path.write_text(text, encoding="utf-8")
    return str(path)


def save_json_result(result_dict: dict, results_dir: Path) -> str:
    path = json_result_path(results_dir)
    path.write_text(json.dumps(result_dict, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(path)
