"""The trial gate: the full book is processed only after the trial passes objective checks."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from book_dashboard.model import PipelineError
from book_dashboard.reconstruction.job import ACCEPTANCE, read_json, write_json
from book_dashboard.reconstruction.structure import prose

MIN_ACCURACY = 0.99


def edit_distance(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, 1):
        current = [i]
        for j, char_b in enumerate(b, 1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (char_a != char_b))
            )
        previous = current
    return previous[-1]


def prose_accuracy(reference: str, candidate: str) -> float:
    """Character accuracy of *candidate* prose against a manually checked *reference*."""
    reference, candidate = prose(reference), prose(candidate)
    if not reference:
        raise PipelineError("The reference transcription has no prose.")
    return max(0.0, 1 - edit_distance(reference, candidate) / len(reference))


def accept_trial(output: Path, reference_dir: Path, *, equations_reviewed: bool) -> dict:
    if not (output / "job.json").is_file() or not (output / "review.json").is_file():
        raise PipelineError(f"{output} has no completed trial; run the trial first.")
    job = read_json(output / "job.json")
    review = read_json(output / "review.json")
    failures = []
    accuracies = {}
    for page in review["pages"]:
        reference = reference_dir / f"page_{page:04d}.txt"
        if not reference.is_file():
            failures.append(f"no manually checked transcription at {reference}")
            continue
        text = (output / "pages" / f"page_{page:04d}.md").read_text(encoding="utf-8")
        accuracies[str(page)] = prose_accuracy(reference.read_text(encoding="utf-8"), text)
        if accuracies[str(page)] < MIN_ACCURACY:
            failures.append(f"page {page}: prose accuracy {accuracies[str(page)]:.2%} < 99%")
    blocking = [i for i in review["issues"] if i["severity"] == "error"]
    failures += [f"page {i['page']}: {i['kind']}: {i['detail']}" for i in blocking]
    unresolved = [
        i for i in review["issues"] if i["severity"] == "warning" and not i["resolved_by_edit"]
    ]
    failures += [f"page {i['page']}: unreviewed {i['kind']}: {i['detail']}" for i in unresolved]
    if not equations_reviewed:
        failures.append(
            "equations not confirmed: check every equation, then pass --equations-reviewed"
        )
    if not job.get("compiled"):
        failures.append("the last compilation failed")
    if failures:
        raise PipelineError("Trial not accepted:\n- " + "\n- ".join(failures))
    record = {
        "checksum": job["checksum"],
        "accepted_at": datetime.now(UTC).isoformat(),
        "accuracy": accuracies,
        "ocr_seconds": review["ocr_seconds"],
        "peak_rss_mb": review["peak_rss_mb"],
        "pages": review["pages"],
    }
    write_json(output / ACCEPTANCE, record)
    return record
