"""Turn raw OCR output into editable page Markdown plus review issues.

Cleanup is deliberately limited to whitespace. Anything questionable is reported, never
rewritten, so the text stays faithful to the recognized source.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

MATH = re.compile(r"\$\$.+?\$\$|\$[^$\n]+\$", re.DOTALL)
IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
FIGURE_TYPES = {"Picture", "Figure", "PictureGroup", "FigureGroup"}
FULL_PAGE_AREA = 0.5
MIN_PROSE_CHARACTERS = 40


@dataclass(frozen=True)
class Issue:
    page: int
    kind: str
    severity: str  # "error" blocks acceptance; "warning" asks for a look
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


def clean_markdown(markdown: str) -> str:
    """Strip trailing blanks, squeeze repeated spaces outside math, and merge blank-line runs."""

    # Math is swapped for placeholders so spaces around it are squeezed but never inside it.
    formulas: list[str] = []

    def hide(match: re.Match) -> str:
        formulas.append(match.group())
        return f"\x00{len(formulas) - 1}\x00"

    text = re.sub(r"(?<=\S) {2,}(?=\S)", " ", MATH.sub(hide, markdown))
    text = re.sub(r"\x00(\d+)\x00", lambda match: formulas[int(match.group(1))], text)
    text = "\n".join(line.rstrip() for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


def math_fragments(markdown: str) -> list[str]:
    return [match.group().strip("$").strip() for match in MATH.finditer(markdown)]


def math_problem(fragment: str) -> str | None:
    if not fragment:
        return "empty formula"
    stripped = re.sub(r"\\[{}]", "", fragment)
    if stripped.count("{") != stripped.count("}"):
        return "unbalanced braces"
    if fragment.count("\\left") != fragment.count("\\right"):
        return "unbalanced \\left/\\right"
    return None


def prose(markdown: str) -> str:
    """Text without formulas and images, for accuracy measurements and emptiness checks."""
    text = IMAGE.sub("", MATH.sub(" ", markdown))
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    return re.sub(r"[#>*_`|-]+|\s+", " ", text).strip()


def _walk(block: dict):
    yield block
    for child in block.get("children", ()):
        yield from _walk(child)


def full_page_figures(structure: list[dict]) -> list[str]:
    found = []
    for root in structure:
        page = root.get("bbox") or [0, 0, 0, 0]
        page_area = max((page[2] - page[0]) * (page[3] - page[1]), 1e-9)
        for block in _walk(root):
            if block["block_type"] not in FIGURE_TYPES or not block.get("bbox"):
                continue
            x0, y0, x1, y1 = block["bbox"]
            if (x1 - x0) * (y1 - y0) / page_area >= FULL_PAGE_AREA:
                found.append(block["id"])
    return found


def build_page(result: dict) -> tuple[str, list[Issue], dict]:
    """Return (clean markdown, issues, stats) for one OCR result."""
    page = result["page"]
    issues: list[Issue] = []
    markdown = clean_markdown(result.get("markdown", ""))
    images = result.get("images", {})
    flagged = full_page_figures(result.get("structure", []))
    if flagged:
        issues.append(
            Issue(
                page,
                "full_page_figure",
                "error",
                "A picture covers most of the page, so its prose was not recognized: "
                + ", ".join(flagged),
            )
        )

    def place(match: re.Match) -> str:
        alt, name = match.groups()
        if name not in images:
            issues.append(Issue(page, "missing_figure", "error", f"No cropped file for {name}"))
            return f"<!-- REVIEW page {page}: missing figure {name} -->"
        if flagged:
            return f"<!-- REVIEW page {page}: full-page picture {name} not embedded -->"
        return f"![{alt}]({images[name]['path']})"

    markdown = IMAGE.sub(place, markdown)
    if len(prose(markdown)) < MIN_PROSE_CHARACTERS and not math_fragments(markdown):
        issues.append(Issue(page, "no_text", "error", "Almost no text was recognized."))
    fragments = math_fragments(markdown)
    for number, fragment in enumerate(fragments, 1):
        problem = math_problem(fragment)
        if problem:
            issues.append(
                Issue(
                    page, "suspicious_math", "warning", f"Formula {number}: {problem}: {fragment}"
                )
            )
    stats = {
        "prose_characters": len(prose(markdown)),
        "formulas": len(fragments),
        "figures": len(images) if not flagged else 0,
        "seconds": result.get("seconds"),
        "peak_rss_mb": result.get("peak_rss_mb"),
    }
    return f"<!-- page {page} -->\n\n{markdown}", issues, stats
