"""Marker OCR worker. Runs inside the isolated reconstruction environment (Python 3.12).

It must stay standalone: it cannot import ``book_dashboard`` because the dashboard's own
environment is a different interpreter. Each finished page is announced as one JSON line on
stdout so the caller can checkpoint it immediately.
"""

from __future__ import annotations

import argparse
import json
import resource
import sys
import time
from importlib.metadata import version
from pathlib import Path

OCR_CONFIG = {"force_ocr": True, "use_llm": False, "output_format": "markdown"}
MODEL_PACKAGES = ("marker-pdf", "surya-ocr", "torch")


def model_versions() -> dict[str, str]:
    return {name: version(name) for name in MODEL_PACKAGES}


def block_tree(block: dict) -> dict:
    node = {"id": block["id"], "block_type": block["block_type"], "bbox": block.get("bbox")}
    if block.get("html"):
        node["html"] = block["html"]
    children = block.get("children")
    if children:
        node["children"] = [block_tree(child) for child in children]
    return node


def convert_page(converter, pdf: Path, page: int, out: Path, dpi: int) -> dict:
    import pypdfium2
    from marker.renderers.json import JSONRenderer
    from marker.renderers.markdown import MarkdownRenderer

    converter.config["page_range"] = [page - 1]
    document = converter.build_document(str(pdf))
    markdown = converter.resolve_dependencies(MarkdownRenderer)(document)
    structure = converter.resolve_dependencies(JSONRenderer)(document)
    figures = out / "figures" / f"page_{page:04d}"
    figures.mkdir(parents=True, exist_ok=True)
    images = {}
    for name, image in markdown.images.items():
        image.save(figures / name)
        images[name] = {"path": f"figures/page_{page:04d}/{name}", "size": list(image.size)}
    original = out / "original"
    original.mkdir(parents=True, exist_ok=True)
    pdf_page = pypdfium2.PdfDocument(str(pdf))[page - 1]
    pdf_page.render(scale=dpi / 72).to_pil().convert("RGB").save(original / f"page_{page:04d}.png")
    return {
        "page": page,
        "markdown": markdown.markdown,
        "structure": [block_tree(child.model_dump()) for child in structure.children],
        "images": images,
        "original_png": f"original/page_{page:04d}.png",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--pages", type=int, nargs="+")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--dpi", type=int, default=110)
    parser.add_argument(
        "--describe", action="store_true", help="Print config, versions, page count"
    )
    args = parser.parse_args()
    if args.describe:
        import pypdfium2

        document = pypdfium2.PdfDocument(str(args.pdf))
        description = {
            "config": OCR_CONFIG,
            "models": model_versions(),
            "page_count": len(document),
        }
        print(json.dumps(description))
        return 0
    if not args.pages or not args.out:
        parser.error("--pages and --out are required")

    from marker.converters.pdf import PdfConverter
    from marker.models import create_model_dict

    converter = PdfConverter(artifact_dict=create_model_dict(), config=dict(OCR_CONFIG))
    for page in args.pages:
        started = time.monotonic()
        result = convert_page(converter, args.pdf, page, args.out, args.dpi)
        result["seconds"] = round(time.monotonic() - started, 1)
        result["peak_rss_mb"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
        target = args.out / "ocr" / f"page_{page:04d}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        print(json.dumps({"page": page, "result": str(target)}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
