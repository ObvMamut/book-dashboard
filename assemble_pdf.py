#!/usr/bin/env python3
"""Render existing local downloads without requesting a token or contacting the viewer."""

from __future__ import annotations

import argparse
import json
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from book_dashboard.model import PipelineError
from book_dashboard.rendering import build_combined_html, check_chromium, render_pdf


def load_pages(directory: Path) -> list[Path]:
    manifest_path = directory / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, list) or any(not entry.get("validated") for entry in manifest):
            raise PipelineError("The legacy page manifest contains unvalidated pages.")
        numbers = sorted(entry["requested_page"] for entry in manifest)
        files = [directory / f"page_{number:04d}.html" for number in numbers]
    else:
        files = sorted(
            (
                path
                for path in directory.glob("page_*.html")
                if re.fullmatch(r"page_\d+\.html", path.name)
            ),
            key=lambda path: int(path.stem.split("_")[1]),
        )
    if not files or any(not path.is_file() for path in files):
        raise PipelineError("No complete set of page files was found.")
    return files


def main() -> int:
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages", type=Path, default=root / "pages")
    parser.add_argument("--assets", type=Path, default=root / "assets")
    parser.add_argument(
        "--output", type=Path, help="A new output directory; existing files are preserved"
    )
    parser.add_argument(
        "--inspect-only",
        action="store_true",
        help="Check fonts, images, and source resolution without printing a PDF",
    )
    args = parser.parse_args()
    try:
        pages = load_pages(args.pages.resolve())
        stylesheets = sorted(args.assets.resolve().glob("*.css"))
        if not stylesheets:
            raise PipelineError("No stylesheets were found.")
        check_chromium()
        output = args.output or root / "jobs" / (
            "local-" + datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8]
        )
        output = output.resolve()
        output.mkdir(parents=True, exist_ok=False)
        cancel = threading.Event()
        path, width, height = build_combined_html(
            pages, stylesheets, output / "combined.html", cancel
        )
        render_pdf(
            path,
            width,
            height,
            output / "book.pdf",
            cancel,
            lambda event: print(event.message),
            inspect_only=args.inspect_only,
        )
        if args.inspect_only:
            print(f"Quality report ready: {output / 'quality-report.json'}")
        else:
            print(f"PDF ready: {output / 'book.pdf'} ({len(pages)} source pages)")
        return 0
    except (PipelineError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Rendering failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
