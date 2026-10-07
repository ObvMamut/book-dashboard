from __future__ import annotations

from book_dashboard.model import PipelineError

TRIAL_PAGES = (10, 20)


def parse_pages(spec: str | None, *, total: int, all_pages: bool = False) -> list[int]:
    """Parse inclusive, one-based PDF page numbers such as ``10,20`` or ``5-8``."""
    if all_pages:
        if spec is not None:
            raise PipelineError("Use either --pages or --all-pages, not both.")
        return list(range(1, total + 1))
    if spec is None:
        spec = ",".join(str(page) for page in TRIAL_PAGES)
    pages: set[int] = set()
    for part in spec.split(","):
        first, dash, last = part.strip().partition("-")
        if not first.isdigit() or (dash and not last.isdigit()):
            raise PipelineError(f"Invalid page selection {part!r}; use numbers like 10,20 or 5-8.")
        start, end = int(first), int(last) if dash else int(first)
        if start < 1 or end < start or end > total:
            raise PipelineError(f"Page selection {part!r} is outside pages 1-{total}.")
        pages.update(range(start, end + 1))
    return sorted(pages)
