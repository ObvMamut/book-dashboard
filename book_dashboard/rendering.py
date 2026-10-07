"""Assemble local fragments and print with strict font / image readiness checks."""

from __future__ import annotations

import html
import json
import re
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from urllib.parse import unquote, urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from .downloads import check_cancel
from .model import PipelineError, Progress
from .quality import IMAGE_METRICS, LOW_PPI, quality_report
from .storage import atomic_write

Emit = Callable[[Progress], None]


def check_chromium():
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            browser.close()
    except PlaywrightError as exc:
        raise PipelineError(
            "Chromium could not start. Run: python -m playwright install chromium. "
            "On Linux, missing system libraries may require playwright install-deps chromium."
        ) from exc


def print_rules(css: str) -> str:
    """Extract print media blocks, allowing whitespace and nested rules."""
    blocks = []
    for match in re.finditer(r"@media\s+[^{}]*\bprint\b[^{}]*\{", css, re.IGNORECASE):
        start = match.end()
        depth = 1
        end = start
        while end < len(css) and depth:
            depth += (css[end] == "{") - (css[end] == "}")
            end += 1
        if depth == 0:
            blocks.append(css[start : end - 1])
    return "\n".join(blocks)


def page_classes(fragment: str) -> tuple[str, str]:
    match = re.search(r"class\s*=\s*['\"]([^'\"]*\bpf\b[^'\"]*)['\"]", fragment)
    if match:
        classes = match[1].split()
        widths = [name for name in classes if re.fullmatch(r"w[\da-z]+", name)]
        heights = [name for name in classes if re.fullmatch(r"h[\da-z]+", name)]
        if widths and heights:
            return widths[0], heights[0]
    raise PipelineError("Could not find the page container's width and height classes.")


def detect_page_size(fragment: str, css_files: Sequence[Path]) -> tuple[str, str]:
    width_class, height_class = page_classes(fragment)
    css = "\n".join(print_rules(path.read_text(encoding="utf-8")) for path in css_files)
    dimensions = []
    for classname, property_name in ((width_class, "width"), (height_class, "height")):
        pattern = (
            rf"\.{re.escape(classname)}(?=[\s,{{])[^{{}}]*\{{[^{{}}]*?"
            rf"\b{property_name}\s*:\s*([\d.]+)\s*(pt|px|in|mm|cm)"
        )
        matches = re.findall(pattern, css, re.IGNORECASE)
        if not matches or float(matches[-1][0]) <= 0:
            raise PipelineError(f"No valid print {property_name} rule for .{classname}.")
        dimensions.append("".join(matches[-1]).lower())
    return dimensions[0], dimensions[1]


def to_css_px(value: str) -> str:
    match = re.fullmatch(r"([\d.]+)(pt|px|in|mm|cm)", value)
    if match is None:
        raise PipelineError("Unsupported PDF page dimension.")
    return f"{float(match[1]) * 96 / 72:.4f}px" if match[2] == "pt" else value


def build_combined_html(
    page_files: Sequence[Path],
    css_files: Sequence[Path],
    output: Path,
    cancel: threading.Event | None = None,
) -> tuple[Path, str, str]:
    if not page_files or not css_files:
        raise PipelineError("Rendering requires both pages and stylesheets.")
    cancel = cancel or threading.Event()
    fragments = []
    for path in page_files:
        check_cancel(cancel)
        fragments.append(path.read_text(encoding="utf-8"))
    width, height = detect_page_size(fragments[0], css_files)
    # Fail explicitly rather than shrinking or clipping a book with mixed page sizes.
    checked_classes = {page_classes(fragments[0])}
    for fragment in fragments:
        classes = page_classes(fragment)
        if classes not in checked_classes:
            if detect_page_size(fragment, css_files) != (width, height):
                raise PipelineError(
                    "This book has mixed page dimensions; a single size would clip it."
                )
            checked_classes.add(classes)
    links = "\n".join(
        f'<link rel="stylesheet" href="{html.escape(path.resolve().as_uri(), quote=True)}">'
        for path in css_files
    )
    document = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
{links}
<style>
@page {{ size: {width} {height}; margin: 0; }}
html, body {{ margin: 0; padding: 0; }}
.pf {{ position: relative; page-break-after: always; overflow: hidden;
       margin: 0; contain: strict; }}
.pf:last-child {{ page-break-after: auto; }}
</style></head><body>{"".join(fragments)}</body></html>"""
    atomic_write(output, document)
    return output, width, height


def render_pdf(
    html_path: Path,
    width: str,
    height: str,
    output: Path,
    cancel: threading.Event,
    emit: Emit,
    *,
    inspect_only: bool = False,
):
    """Inspect print layout and optionally print atomically, preserving source content."""
    partial = output.with_name(".book.partial.pdf")
    report_path = output.with_name("quality-report.json")
    try:
        check_cancel(cancel)
        if output.exists() and not inspect_only:
            raise PipelineError("The destination PDF already exists; it was preserved.")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page()
                failures = []

                def record_failure(request):
                    url = urlsplit(request.url)
                    name = (
                        Path(unquote(url.path)).name
                        if url.scheme == "file"
                        else request.resource_type
                    )
                    failures.append(name)

                page.on("requestfailed", record_failure)

                def restrict_network(route):
                    if url_scheme(route.request.url) in ("file", "data", "about", "blob"):
                        route.continue_()
                    else:
                        failures.append("external resource")
                        route.abort()

                page.route("**/*", restrict_network)
                # Avoid Chromium's pending-request limit on books with thousands of fonts.
                # Hide the body before its first layout, then preload font faces in batches.
                page.add_init_script("""(() => {
                    const style = document.createElement('style');
                    style.id = 'book-font-preload';
                    style.textContent = 'body { display: none !important; }';
                    const install = () => {
                        if (!document.documentElement) return false;
                        document.documentElement.appendChild(style);
                        return true;
                    };
                    if (!install()) {
                        const observer = new MutationObserver(() => {
                            if (install()) observer.disconnect();
                        });
                        observer.observe(document, {childList: true});
                    }
                })();""")
                check_cancel(cancel)
                emit(Progress("render", "Loading combined HTML in Chromium."))
                page.goto(html_path.resolve().as_uri(), wait_until="load", timeout=300000)
                page.emulate_media(media="print")
                # Force all declared font faces to load, including fonts in print-only rules.
                page.evaluate("""() => {
                    window.bookFontsReady = false;
                    window.bookFontFailure = false;
                    window.bookFonts = (async () => {
                        const faces = Array.from(document.fonts);
                        for (let index = 0; index < faces.length; index += 64) {
                            const results = await Promise.allSettled(
                                faces.slice(index, index + 64).map(face => face.load())
                            );
                            if (results.some(result => result.status === 'rejected')) {
                                window.bookFontFailure = true;
                            }
                        }
                        document.getElementById('book-font-preload').remove();
                        window.bookFontsReady = true;
                    })();
                }""")
                check_cancel(cancel)
                emit(Progress("render", "Waiting for fonts and images."))
                page.wait_for_function(
                    "window.bookFontsReady",
                    timeout=120000,
                )
                page.wait_for_function(
                    "Array.from(document.images).every(image => image.complete)",
                    timeout=120000,
                )
                ready = page.evaluate("""() => ({
                    fonts: !window.bookFontFailure &&
                        Array.from(document.fonts).every(face => face.status === 'loaded'),
                    images: Array.from(document.images).every(image => image.naturalWidth > 0)
                })""")
                if failures or not ready["fonts"] or not ready["images"]:
                    details = ", ".join(sorted(set(failures))[:3]) or "font or image decoding"
                    raise PipelineError(f"Required resources failed to load: {details}.")
                check_cancel(cancel)
                report = quality_report(page.evaluate(IMAGE_METRICS), width, height)
                report["inspection_only"] = inspect_only
                report["fonts_loaded"] = True
                atomic_write(report_path, json.dumps(report, indent=2, allow_nan=False))
                low_pages = report["low_resolution_pages"]
                if low_pages:
                    examples = ", ".join(map(str, low_pages[:5]))
                    emit(
                        Progress(
                            "quality",
                            f"Source images below {LOW_PPI} PPI on {len(low_pages)} pages "
                            f"(examples: {examples}). Source detail limits sharpness. "
                            f"Report: {report_path}",
                        )
                    )
                else:
                    emit(Progress("quality", f"Source quality inspection saved: {report_path}"))
                if report["unmeasured_pages"]:
                    emit(
                        Progress(
                            "quality",
                            "Some transformed images could not be measured; "
                            "see the quality report.",
                        )
                    )
                check_cancel(cancel)
                if inspect_only:
                    return report
                emit(Progress("render", "Printing PDF. Cancellation waits for Chromium to finish."))
                page.pdf(
                    path=str(partial),
                    width=to_css_px(width),
                    height=to_css_px(height),
                    print_background=True,
                    prefer_css_page_size=True,
                    scale=1,
                    margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
                )
                check_cancel(cancel)
                with partial.open("rb") as file:
                    valid_pdf = file.read(5) == b"%PDF-"
                if not valid_pdf:
                    raise PipelineError("Chromium did not produce a valid PDF.")
                if output.exists():
                    raise PipelineError("The destination PDF already exists; it was preserved.")
                partial.replace(output)
                return report
            finally:
                browser.close()
    except PlaywrightError as exc:
        raise PipelineError(
            "Chromium rendering failed or timed out; no PDF was published."
        ) from exc
    finally:
        partial.unlink(missing_ok=True)


def url_scheme(url: str) -> str:
    return url.partition(":")[0].lower()
