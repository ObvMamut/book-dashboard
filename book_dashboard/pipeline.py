"""One application-level workflow with explicit terminal outcomes."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .downloads import HttpClient, assets_intact, check_cancel, download_assets, download_pages
from .model import AuthenticationRequired, Cancelled, Config, PipelineError, Progress
from .rendering import build_combined_html, check_chromium, render_pdf
from .storage import Job, intact


@dataclass(frozen=True)
class Result:
    status: str
    job_path: Path | None = None
    pdf_path: Path | None = None
    error: str = ""


def run(
    config: Config,
    emit: Callable[[Progress], None],
    cancel: threading.Event,
    job_path: Path | None = None,
    *,
    preflight: Callable = check_chromium,
    renderer: Callable = render_pdf,
    client_factory: Callable = HttpClient,
) -> Result:
    job = None
    client = None

    def progress(event: Progress):
        emit(Progress(event.stage, config.redact(event.message), event.completed, event.total))

    try:
        check_cancel(cancel)
        # Validate resume identity before launching Chromium or making any request.
        if job_path is not None:
            job = Job.load(job_path, config)
        progress(Progress("validate", "Checking Chromium and output settings."))
        preflight()
        check_cancel(cancel)
        if job is None:
            job = Job.create(config)
        job.manifest["settings"] = config.settings()
        job.status("running", "pages")
        progress(Progress("pages", f"Job directory: {job.path}"))
        client = client_factory(config, cancel, progress)
        download_pages(job, config, client, progress)
        job.status("running", "assets")
        download_assets(job, config, client, progress)
        check_cancel(cancel)
        if not assets_intact(job, config) or not all(
            intact(job.page_path(n), job.manifest["pages"].get(str(n)))
            for n in range(config.first_page, config.last_page + 1)
        ):
            raise PipelineError("Downloads failed their integrity check. Resume to repair the job.")
        job.status("running", "assemble")
        progress(Progress("assemble", "Assembling pages in numeric order."))
        pages = [
            job.page_path(n, rendered=True) for n in range(config.first_page, config.last_page + 1)
        ]
        css = [job.asset_path(name) for name in job.manifest["stylesheets"]]
        path, width, height = build_combined_html(pages, css, job.path / "combined.html", cancel)
        job.status("running", "render")
        pdf_path = job.path / "book.pdf"
        # A crash after publishing may leave a valid PDF with a running checkpoint.
        # Preserve it and ask the user to start a new job instead of overwriting it.
        if pdf_path.exists():
            raise PipelineError(
                "This job already contains a PDF; it was preserved. Start a new job."
            )
        renderer(path, width, height, pdf_path, cancel, progress)
        job.status("completed", "completed")
        progress(Progress("completed", f"PDF ready: {pdf_path}", 1, 1))
        return Result("completed", job.path, pdf_path)
    except AuthenticationRequired as exc:
        status, message = "paused", str(exc)
    except Cancelled as exc:
        status, message = "cancelled", str(exc)
    except (PipelineError, OSError) as exc:
        status, message = "failed", str(exc)
    except Exception as exc:
        # This is the application boundary: unexpected failures remain visible as failures.
        status, message = "failed", f"Unexpected {type(exc).__name__}: {exc}"
    finally:
        if client is not None:
            client.close()
    message = config.redact(message)
    if job is not None:
        job.status(status)
    if status == "paused":
        message += " Paste fresh credentials, then select Resume."
    progress(Progress(status, message))
    return Result(status, job.path if job else None, error=message)
