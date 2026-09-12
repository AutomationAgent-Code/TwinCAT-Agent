"""Convert a text-based PDF manual into page-level HTML search documents.

The generated files use the small InfoSys-compatible HTML envelope consumed by
ba-docsearch.  Keeping one PDF page per document gives docs_search useful result
granularity and keeps docs_read responses below its normal truncation limit.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import shutil
import tempfile
from pathlib import Path

from pypdf import PdfReader


GENERATOR_VERSION = 1
API_NAME = re.compile(
    r"\b(?:MC|FB|ST|E|Type|ITc|CDT)_[A-Za-z][A-Za-z0-9_]*\b"
    r"|\bITc[A-Za-z0-9_]+\b"
)
NUMBERED_HEADING = re.compile(r"^\d+(?:\.\d+)+\s+.+")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _page_title(text: str, page_number: int, manual_title: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines:
        if NUMBERED_HEADING.match(line):
            return f"{manual_title} - {line[:160]}"
    for line in lines:
        if " | " in line and not line.startswith("Manual  |"):
            return f"{manual_title} - {line[:160]}"
    return f"{manual_title} - Page {page_number}"


def _html_document(
    *,
    title: str,
    text: str,
    page_number: int,
    product: str,
    category: str,
    product_group: str,
    source_name: str,
) -> str:
    api_names = sorted(set(API_NAME.findall(text)), key=str.casefold)
    keywords = " ".join(
        ["TF55xx", "TwinCAT", "MC3", f"page {page_number}", *api_names]
    )
    topic_id = f"TF55xx_TC3_MC3_EN_p{page_number:04d}"
    description = f"{source_name}, PDF page {page_number}"
    escaped = html.escape(text)
    return (
        "<!doctype html>\n<html><head><meta charset=\"utf-8\">\n"
        f"<title>{html.escape(title)}</title>\n"
        f"<meta name=\"topicid\" content=\"{topic_id}\">\n"
        f"<meta name=\"product\" content=\"{html.escape(product)}\">\n"
        f"<meta name=\"category\" content=\"{html.escape(category)}\">\n"
        f"<meta name=\"productgroup\" content=\"{html.escape(product_group)}\">\n"
        f"<meta name=\"description\" content=\"{html.escape(description)}\">\n"
        f"<meta name=\"keywords\" content=\"{html.escape(keywords)}\">\n"
        "</head><body>\n"
        f"<ol class=\"breadcrumb\"><li>TwinCAT</li><li>Motion</li>"
        f"<li>TF55xx MC3</li><li>Page {page_number}</li></ol>\n"
        f"<article><pre>{escaped}</pre></article>\n"
        "</body></html>\n"
    )


def import_manual(
    pdf_path: Path,
    output_dir: Path,
    *,
    title: str,
    product: str,
    category: str,
    product_group: str,
    force: bool = False,
) -> dict[str, object]:
    pdf_path = pdf_path.resolve()
    output_dir = output_dir.resolve()
    if not pdf_path.is_file():
        raise FileNotFoundError(pdf_path)

    source_hash = _sha256(pdf_path)
    manifest_path = output_dir / "manifest.json"
    if not force and manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = {}
        expected_pages = int(manifest.get("pages", 0) or 0)
        existing_pages = len(list(output_dir.glob("TF55xx_TC3_MC3_EN_p*.html")))
        if (
            manifest.get("sha256") == source_hash
            and manifest.get("generator_version") == GENERATOR_VERSION
            and expected_pages > 0
            and existing_pages == expected_pages
        ):
            return {**manifest, "status": "unchanged", "output": str(output_dir)}

    reader = PdfReader(str(pdf_path))
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.", dir=output_dir.parent))
    try:
        empty_pages = 0
        for index, page in enumerate(reader.pages, start=1):
            text = (page.extract_text() or "").strip()
            if not text:
                empty_pages += 1
                text = f"{title}\nSource PDF page {index} contains no extractable text."
            page_title = _page_title(text, index, title)
            document = _html_document(
                title=page_title,
                text=text,
                page_number=index,
                product=product,
                category=category,
                product_group=product_group,
                source_name=pdf_path.name,
            )
            target = staging / f"TF55xx_TC3_MC3_EN_p{index:04d}.html"
            target.write_text(document, encoding="utf-8", newline="\n")

        manifest = {
            "generator_version": GENERATOR_VERSION,
            "source": pdf_path.name,
            "sha256": source_hash,
            "pages": len(reader.pages),
            "empty_pages": empty_pages,
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if output_dir.exists():
            shutil.rmtree(output_dir)
        staging.replace(output_dir)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {**manifest, "status": "imported", "output": str(output_dir)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--title", default="TF55xx TwinCAT 3 MC3")
    parser.add_argument("--product", default="TF55xx TwinCAT 3 MC3")
    parser.add_argument("--category", default="TwinCAT Motion")
    parser.add_argument("--product-group", default="Motion")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    result = import_manual(
        args.pdf,
        args.output,
        title=args.title,
        product=args.product,
        category=args.category,
        product_group=args.product_group,
        force=args.force,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
