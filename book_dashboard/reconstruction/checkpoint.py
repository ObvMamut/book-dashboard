from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


def file_checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def checkpoint_key(*, checksum: str, page: int, config: dict, models: dict) -> str:
    payload = json.dumps(
        {"checksum": checksum, "page": page, "config": config, "models": models},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


class CheckpointStore:
    """One JSON file per page; a result is reused only when its key still matches."""

    def __init__(self, directory: Path):
        self.directory = directory

    def path(self, page: int) -> Path:
        return self.directory / f"page_{page:04d}.json"

    def load(self, page: int, key: str) -> dict | None:
        try:
            record = json.loads(self.path(page).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(record, dict) or record.get("key") != key:
            return None
        return record.get("result")

    def save(self, page: int, key: str, result: dict) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.path(page)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps({"key": key, "result": result}), encoding="utf-8")
        os.replace(temporary, target)
