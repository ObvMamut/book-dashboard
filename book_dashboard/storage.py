"""Atomic checkpoints and integrity checks; credentials are never serialized here."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .model import Config, PipelineError


def atomic_write(path: Path, content: bytes | str):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode("utf-8") if isinstance(content, str) else content
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".partial-", delete=False) as file:
            temporary = Path(file.name)
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def file_record(path: Path) -> dict:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def intact(path: Path, record: dict | None) -> bool:
    return bool(
        record
        and path.is_file()
        and path.stat().st_size == record.get("bytes")
        and file_record(path)["sha256"] == record.get("sha256")
    )


def asset_key(url: str, token: str) -> str:
    normalized = url.replace(token, "{BOOK_TOKEN}")
    return hashlib.sha256(normalized.encode()).hexdigest()


def asset_name(url: str, token: str) -> str:
    from urllib.parse import urlsplit

    suffix = Path(urlsplit(url).path).suffix.lower()
    if not suffix or len(suffix) > 10 or not suffix[1:].isalnum():
        suffix = ".bin"
    return f"asset_{asset_key(url, token)}{suffix}"


class Job:
    def __init__(self, path: Path, manifest: dict):
        self.path = path
        self.manifest = manifest

    @classmethod
    def create(cls, config: Config) -> Job:
        name = datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8]
        path = config.output_dir / name
        path.mkdir(parents=True, exist_ok=False)
        job = cls(
            path,
            {
                "version": 1,
                "identity": config.identity,
                "settings": config.settings(),
                "status": "ready",
                "stage": "validate",
                "pages": {},
                "assets": {},
                "render_pages": {},
                "stylesheets": [],
                "assets_complete": False,
            },
        )
        job.save()
        return job

    @classmethod
    def load(cls, path: Path, config: Config | None = None) -> Job:
        try:
            manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
            if (
                manifest["version"] != 1
                or not isinstance(manifest["identity"], dict)
                or not isinstance(manifest["settings"], dict)
                or any(
                    not isinstance(manifest[key], dict)
                    for key in ("pages", "assets", "render_pages")
                )
                or not isinstance(manifest["stylesheets"], list)
            ):
                raise ValueError
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise PipelineError(
                "This job has a missing, corrupt, or unsupported checkpoint."
            ) from exc
        if config is not None and manifest["identity"] != config.identity:
            raise PipelineError("Resume requires the same host, book identifier, and page range.")
        if manifest.get("status") == "completed":
            raise PipelineError("This job is already complete. Start a new job instead.")
        return cls(path.resolve(), manifest)

    def save(self):
        atomic_write(self.path / "manifest.json", json.dumps(self.manifest, indent=2))

    def status(self, status: str, stage: str | None = None):
        self.manifest["status"] = status
        if stage is not None:
            self.manifest["stage"] = stage
        self.save()

    def page_path(self, number: int, *, rendered: bool = False) -> Path:
        return self.path / ("render_pages" if rendered else "pages") / f"page_{number:04d}.html"

    def asset_path(self, filename: str) -> Path:
        if Path(filename).name != filename or filename in ("", ".", ".."):
            raise PipelineError("Invalid asset filename in checkpoint.")
        return self.path / "assets" / filename


def list_jobs(root: Path) -> list[Job]:
    jobs = []
    for checkpoint in sorted(root.glob("*/manifest.json"), reverse=True):
        try:
            jobs.append(Job.load(checkpoint.parent))
        except PipelineError:
            # Unreadable and completed jobs cannot be selected for resumption.
            continue
    return jobs
