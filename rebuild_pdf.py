#!/usr/bin/env python3
"""Rebuild a PDF as selectable text and LaTeX math (local OCR, Pandoc, XeLaTeX)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from book_dashboard.model import PipelineError
from book_dashboard.reconstruction.acceptance import accept_trial
from book_dashboard.reconstruction.job import DEFAULT_PYTHON, JobOptions, WorkerBackend, run_job


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, nargs="?", help="The source PDF (never modified)")
    parser.add_argument("--output", type=Path, required=True, help="Job directory")
    parser.add_argument(
        "--pages", help="Inclusive one-based pages, e.g. 10,20 or 5-8 (default 10,20)"
    )
    parser.add_argument("--all-pages", action="store_true", help="Process the whole book")
    parser.add_argument("--resume", action="store_true", help="Continue an existing job directory")
    parser.add_argument(
        "--compile-only",
        action="store_true",
        help="Recompile from the edited sources without running OCR",
    )
    parser.add_argument("--title", help="Document title")
    parser.add_argument("--python", type=Path, default=DEFAULT_PYTHON, help="Reconstruction Python")
    parser.add_argument(
        "--accept-trial",
        action="store_true",
        help="Check the trial against reference/page_NNNN.txt and unlock --all-pages",
    )
    parser.add_argument("--reference", type=Path, help="Directory of manual transcriptions")
    parser.add_argument("--equations-reviewed", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.accept_trial:
            record = accept_trial(
                args.output,
                args.reference or args.output / "reference",
                equations_reviewed=args.equations_reviewed,
            )
            print(f"Trial accepted: {record['accuracy']}. Run again with --resume --all-pages.")
            return 0
        if args.input is None:
            parser.error("the input PDF is required")
        job = run_job(
            JobOptions(
                pdf=args.input.resolve(),
                output=args.output.resolve(),
                pages=args.pages,
                all_pages=args.all_pages,
                resume=args.resume,
                compile_only=args.compile_only,
                title=args.title,
                backend=WorkerBackend(args.python),
            )
        )
    except PipelineError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"Wrote {args.output / 'rebuilt.pdf'} (review: {args.output / 'review.html'})")
    print(f"OCR time {job.get('ocr_seconds')} s, peak memory {job.get('peak_rss_mb')} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
