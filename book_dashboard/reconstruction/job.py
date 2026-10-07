from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from book_dashboard.model import PipelineError
from book_dashboard.reconstruction.checkpoint import CheckpointStore, checkpoint_key, file_checksum
from book_dashboard.reconstruction.compile import compile_book
from book_dashboard.reconstruction.pages import parse_pages
from book_dashboard.reconstruction.review import write_review_html
from book_dashboard.reconstruction.structure import Issue, build_page

WORKER = Path(__file__).with_name("ocr_worker.py")
DEFAULT_PYTHON = Path(__file__).resolve().parents[2] / ".recon-env" / "bin" / "python"
ACCEPTANCE = "trial-accepted.json"


class WorkerBackend:
    """Runs the Marker worker as a subprocess of the isolated reconstruction environment."""

    def __init__(self, python: Path = DEFAULT_PYTHON):
        self.python = python

    def _command(self, *arguments: str) -> list[str]:
        if not self.python.is_file():
            raise PipelineError(
                f"The reconstruction environment {self.python} does not exist; see the README "
                "section 'Rebuild the PDF from OCR' to create it."
            )
        return [str(self.python), str(WORKER), *arguments]

    def describe(self, pdf: Path) -> dict:
        done = subprocess.run(self._command(str(pdf), "--describe"), capture_output=True, text=True)
        if done.returncode:
            raise PipelineError(f"Could not inspect {pdf}:\n{done.stderr.strip()[-2000:]}")
        return json.loads(done.stdout.strip().splitlines()[-1])

    def run(self, pdf: Path, pages: list[int], out: Path) -> Iterator[dict]:
        (out / "logs").mkdir(parents=True, exist_ok=True)
        command = self._command(str(pdf), "--pages", *map(str, pages), "--out", str(out))
        environment = {**os.environ, "TORCH_DEVICE": "cpu", "OMP_NUM_THREADS": str(os.cpu_count())}
        with (out / "logs" / "ocr.log").open("a", encoding="utf-8") as log:
            process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=log, text=True, env=environment
            )
            for line in process.stdout:
                record = json.loads(line)
                yield json.loads(Path(record["result"]).read_text(encoding="utf-8"))
            if process.wait():
                raise PipelineError(
                    f"OCR worker failed (exit {process.returncode}); see {log.name}"
                )


@dataclass
class JobOptions:
    pdf: Path
    output: Path
    pages: str | None = None
    all_pages: bool = False
    resume: bool = False
    compile_only: bool = False
    title: str | None = None
    backend: object = field(default_factory=WorkerBackend)
    compiler: Callable[..., Path] = compile_book
    log: Callable[[str], None] = print


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def prepare(options: JobOptions) -> tuple[dict, str]:
    """Validate the output directory and return (job record, input checksum)."""
    if not options.pdf.is_file():
        raise PipelineError(f"Input PDF {options.pdf} does not exist.")
    checksum = file_checksum(options.pdf)
    job_file = options.output / "job.json"
    if options.compile_only and not options.resume:
        raise PipelineError("--compile-only needs --resume to reuse an existing job.")
    if not options.output.exists():
        if options.resume:
            raise PipelineError(f"Cannot resume: {options.output} does not exist.")
        if options.all_pages:
            raise PipelineError(
                "Run and accept the two-page trial first (--accept-trial), then resume it "
                "with --all-pages."
            )
        options.output.mkdir(parents=True)
        return {"input": str(options.pdf), "checksum": checksum}, checksum
    if not options.resume:
        raise PipelineError(
            f"{options.output} already exists; choose a new directory or pass --resume."
        )
    if not job_file.is_file():
        raise PipelineError(f"{options.output} is not a reconstruction job (no job.json).")
    job = read_json(job_file)
    if job.get("checksum") != checksum:
        raise PipelineError("The input PDF differs from the one this job was created from.")
    return job, checksum


def assemble_book(output: Path, pages: list[int], job: dict) -> None:
    """Write book.md from the editable page files, never replacing hand edits to book.md."""
    book = output / "book.md"
    if book.exists() and sha256_text(book.read_text(encoding="utf-8")) != job.get("book_md_sha256"):
        raise PipelineError(
            "book.md has manual edits that regeneration would discard. Move the corrections "
            "into pages/page_NNNN.md, or run with --compile-only to compile book.md as it is."
        )
    parts = [
        (output / "pages" / f"page_{page:04d}.md").read_text(encoding="utf-8") for page in pages
    ]
    text = "\n".join(parts)
    book.write_text(text, encoding="utf-8")
    job["book_md_sha256"] = sha256_text(text)


def run_job(options: JobOptions) -> dict:
    output = options.output
    job, checksum = prepare(options)
    job_file = output / "job.json"
    started = time.monotonic()
    if options.compile_only:
        pages = job.get("selected_pages")
        if not pages:
            raise PipelineError("The job has no processed pages to compile.")
        book = output / "book.md"
        if not book.exists() or sha256_text(book.read_text(encoding="utf-8")) == job.get(
            "book_md_sha256"
        ):
            assemble_book(output, pages, job)
    else:
        description = options.backend.describe(options.pdf)
        pages = parse_pages(
            options.pages, total=description["page_count"], all_pages=options.all_pages
        )
        if options.all_pages:
            gate = output / ACCEPTANCE
            if not gate.is_file() or read_json(gate).get("checksum") != checksum:
                raise PipelineError(
                    f"The trial has not been accepted for this PDF; run --accept-trial first "
                    f"({gate} is missing)."
                )
        store = CheckpointStore(output / "checkpoints")
        keys = {
            page: checkpoint_key(
                checksum=checksum,
                page=page,
                config=description["config"],
                models=description["models"],
            )
            for page in pages
        }
        results = {page: store.load(page, keys[page]) for page in pages}
        todo = [page for page in pages if results[page] is None]
        options.log(f"{len(pages) - len(todo)} page(s) reused from checkpoints, {len(todo)} to OCR")
        if todo:
            for result in options.backend.run(options.pdf, todo, output):
                store.save(result["page"], keys[result["page"]], result)
                results[result["page"]] = result
                options.log(f"page {result['page']} recognized in {result.get('seconds')} s")
        missing = [page for page in pages if results[page] is None]
        if missing:
            raise PipelineError(f"The OCR worker returned no result for pages {missing}.")
        write_pages(output, results)
        job.update(
            selected_pages=pages,
            config=description["config"],
            models=description["models"],
            page_count=description["page_count"],
        )
        assemble_book(output, pages, job)
    issues = write_review(output, pages, job)
    job["elapsed_seconds"] = round(time.monotonic() - started, 1)
    write_json(job_file, job)
    options.log(f"{len(issues)} issue(s) recorded in {output / 'review.json'}")
    try:
        options.compiler(output, title=options.title)
        job["compiled"] = True
    except PipelineError:
        job["compiled"] = False
        write_json(job_file, job)
        raise
    write_json(job_file, job)
    return job


def write_pages(output: Path, results: dict[int, dict]) -> None:
    """Create editable page files once; later runs never overwrite corrections."""
    directory = output / "pages"
    directory.mkdir(exist_ok=True)
    generated = {}
    for page, result in results.items():
        markdown, issues, stats = build_page(result)
        target = directory / f"page_{page:04d}.md"
        if not target.exists():
            target.write_text(markdown, encoding="utf-8")
        generated[str(page)] = {
            "sha256": sha256_text(markdown),
            "issues": [issue.to_dict() for issue in issues],
            "stats": stats,
        }
    path = output / "generated.json"
    previous = read_json(path) if path.exists() else {}
    write_json(path, {**previous, **generated})


def write_review(output: Path, pages: list[int], job: dict) -> list[Issue]:
    generated = read_json(output / "generated.json")
    issues, summary, pages_info = [], {"prose_characters": 0, "formulas": 0, "figures": 0}, {}
    seconds, peak = 0.0, 0
    for page in pages:
        record = generated[str(page)]
        text = (output / "pages" / f"page_{page:04d}.md").read_text(encoding="utf-8")
        edited = sha256_text(text) != record["sha256"]
        for issue in record["issues"]:
            issues.append({**issue, "resolved_by_edit": edited and issue["severity"] == "warning"})
        stats = record["stats"]
        for name in summary:
            summary[name] += stats[name]
        seconds += stats.get("seconds") or 0
        peak = max(peak, stats.get("peak_rss_mb") or 0)
        pages_info[str(page)] = {"edited": edited, **stats}
    review = {
        "pages": pages,
        "summary": summary,
        "ocr_seconds": round(seconds, 1),
        "peak_rss_mb": peak,
        "issues": issues,
        "page_details": pages_info,
    }
    write_json(output / "review.json", review)
    write_review_html(output, pages, review)
    job["ocr_seconds"], job["peak_rss_mb"] = review["ocr_seconds"], peak
    return [Issue(**{k: v for k, v in i.items() if k != "resolved_by_edit"}) for i in issues]
