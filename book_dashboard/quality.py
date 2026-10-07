"""Measure source image resolution without modifying content or recording resource URLs."""

from __future__ import annotations

import math

LOW_PPI = 200

# Measure the image's content box along its transformed axes. A bounding rectangle would
# incorrectly report lower resolution for rotated images. Do not return URLs, page text,
# font names, or HTML: any of these can contain session credentials.
IMAGE_METRICS = """() => {
    const pages = Array.from(document.querySelectorAll('.pf'));
    return pages.map((container, index) => {
        const number = container.getAttribute('data-page-no') || '';
        const images = Array.from(container.querySelectorAll('img')).flatMap(image => {
            if (!image.getClientRects().length ||
                getComputedStyle(image).visibility === 'hidden') return [];
            const style = getComputedStyle(image);
            let width = parseFloat(style.width);
            let height = parseFloat(style.height);
            if (style.boxSizing === 'border-box') {
                width -= parseFloat(style.paddingLeft) + parseFloat(style.paddingRight) +
                    parseFloat(style.borderLeftWidth) + parseFloat(style.borderRightWidth);
                height -= parseFloat(style.paddingTop) + parseFloat(style.paddingBottom) +
                    parseFloat(style.borderTopWidth) + parseFloat(style.borderBottomWidth);
            }
            const naturalWidth = image.naturalWidth;
            const naturalHeight = image.naturalHeight;
            if (width <= 0 || height <= 0) return [];
            if (['contain', 'cover', 'scale-down', 'none'].includes(style.objectFit)) {
                let scale = style.objectFit === 'cover'
                    ? Math.max(width / naturalWidth, height / naturalHeight)
                    : Math.min(width / naturalWidth, height / naturalHeight);
                if (style.objectFit === 'none') scale = 1;
                if (style.objectFit === 'scale-down') scale = Math.min(1, scale);
                width = naturalWidth * scale;
                height = naturalHeight * scale;
            }
            let matrix = new DOMMatrix();
            let measurable = true;
            for (let node = image; node; node = node.parentElement) {
                const computed = getComputedStyle(node);
                const transform = new DOMMatrix(
                    computed.transform === 'none' ? undefined : computed.transform
                );
                if (!transform.is2D || computed.perspective !== 'none' ||
                    computed.rotate !== 'none' || computed.scale !== 'none') {
                    measurable = false;
                }
                const zoom = parseFloat(computed.zoom) || 1;
                matrix = transform.scale(zoom).multiply(matrix);
            }
            const source = image.currentSrc || image.src;
            const inline = source.startsWith('data:');
            const svg = /^data:image\\/svg\\+xml[;,]/i.test(source) ||
                (!inline && new URL(source).pathname.toLowerCase().endsWith('.svg'));
            return [{
                kind: svg ? 'vector' : 'raster',
                delivery: inline ? 'inline' : 'local_file',
                pixel_width: svg ? null : naturalWidth,
                pixel_height: svg ? null : naturalHeight,
                printed_width_css_px: measurable ? width * Math.hypot(matrix.a, matrix.b) : null,
                printed_height_css_px: measurable ? height * Math.hypot(matrix.c, matrix.d) : null
            }];
        });
        return {
            page: /^[0-9a-f]+$/i.test(number) && number.length <= 8
                ? parseInt(number, 16) : index + 1,
            html_text_present: container.textContent.trim().length > 0,
            images
        };
    });
}"""


def effective_ppi(pixels: int, css_px: float | None) -> float | None:
    """CSS physical units use 96 px per inch; print scale is fixed to 1."""
    if css_px is None or not math.isfinite(css_px) or css_px <= 0:
        return None
    return round(pixels * 96 / css_px, 2)


def quality_report(pages: list[dict], width: str, height: str) -> dict:
    low_pages = []
    unmeasured_pages = []
    for page in pages:
        low = False
        unmeasured = False
        for image in page["images"]:
            if image["kind"] == "vector":
                image["ppi_x"] = image["ppi_y"] = None
                image["low_resolution"] = False
                continue
            image["ppi_x"] = effective_ppi(image["pixel_width"], image["printed_width_css_px"])
            image["ppi_y"] = effective_ppi(image["pixel_height"], image["printed_height_css_px"])
            measured = [image[key] for key in ("ppi_x", "ppi_y") if image[key] is not None]
            image["low_resolution"] = bool(measured and min(measured) < LOW_PPI)
            low |= image["low_resolution"]
            unmeasured |= len(measured) != 2
        if low:
            low_pages.append(page["page"])
        if unmeasured:
            unmeasured_pages.append(page["page"])
    return {
        "version": 1,
        "source": "viewer_html",
        "higher_quality_source": "not_verified",
        "page_dimensions": {"width": width, "height": height, "source": "viewer_print_css"},
        "print_scale": 1,
        "low_ppi_threshold": LOW_PPI,
        "low_resolution_pages": low_pages,
        "unmeasured_pages": unmeasured_pages,
        "coverage": (
            "Visible HTML img elements; CSS backgrounds and SVG internals are not measured."
        ),
        "source_check": (
            "This offline inspection does not establish whether the live viewer offers a better "
            "source or verify original physical page dimensions."
        ),
        "limitation": (
            "Printing preserves available source detail. Rasterized text and figures cannot "
            "gain genuine detail without a better source. PPI is advisory, not a quality score."
        ),
        "pages": pages,
    }
