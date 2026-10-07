from __future__ import annotations

import html
import subprocess
from pathlib import Path

from book_dashboard.reconstruction.structure import math_fragments

STYLE = """
body{font:15px/1.5 system-ui,sans-serif;margin:0;background:#f4f4f4;color:#111}
header{padding:12px 20px;background:#fff;border-bottom:1px solid #ccc}
section{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;padding:12px 20px;
  border-bottom:2px solid #999}
section>div{background:#fff;padding:10px;overflow:auto;max-height:95vh}
img.original{width:100%}pre{white-space:pre-wrap;font-size:13px}
.error{color:#b00020;font-weight:600}.warning{color:#8a5a00}
ol.equations li{margin-bottom:6px}
"""


def render_fragment(markdown: str) -> str:
    """Render a page's Markdown with MathML (no JavaScript or network access needed)."""
    try:
        done = subprocess.run(
            [
                "pandoc",
                "--from=markdown+tex_math_dollars-yaml_metadata_block",
                "--to=html5",
                "--mathml",
            ],
            input=markdown,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return f"<pre>{html.escape(markdown)}</pre>"
    return done.stdout if done.returncode == 0 else f"<pre>{html.escape(markdown)}</pre>"


def write_review_html(output: Path, pages: list[int], review: dict) -> Path:
    sections = []
    for page in pages:
        text = (output / "pages" / f"page_{page:04d}.md").read_text(encoding="utf-8")
        issues = [i for i in review["issues"] if i["page"] == page]
        listed = (
            "".join(
                f"<li class='{i['severity']}'>{html.escape(i['kind'])}: {html.escape(i['detail'])}"
                f"{' (edited)' if i.get('resolved_by_edit') else ''}</li>"
                for i in issues
            )
            or "<li>No automatic issues.</li>"
        )
        equations = "".join(f"<li><code>{html.escape(f)}</code></li>" for f in math_fragments(text))
        sections.append(
            f"<section id='p{page}'>"
            f"<div><h2>Original, page {page}</h2>"
            f"<img class='original' src='original/page_{page:04d}.png' "
            f"alt='Original page {page}'></div>"
            f"<div><h2>Reconstructed</h2>{render_fragment(text)}</div>"
            f"<div><h2>Issues</h2><ul>{listed}</ul>"
            f"<h2>Equations to check</h2><ol class='equations'>{equations or '<li>None</li>'}</ol>"
            f"<h2>Editable source</h2><pre>pages/page_{page:04d}.md</pre></div></section>"
        )
    summary = review["summary"]
    document = (
        "<!doctype html><meta charset='utf-8'><title>Review</title>"
        f"<style>{STYLE}</style><header><b>Review of pages {pages[0]}–{pages[-1]}</b> · "
        f"{summary['formulas']} formulas · {len(review['issues'])} automatic issues · "
        f"{review['ocr_seconds']} s OCR · peak {review['peak_rss_mb']} MB</header>"
        + "".join(sections)
    )
    target = output / "review.html"
    target.write_text(document, encoding="utf-8")
    return target
