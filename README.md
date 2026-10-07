# Book dashboard

A local terminal dashboard that connects page downloading, asset downloading, and PDF rendering.
Launch it once, enter your settings, and select **Start**. The dashboard runs all three stages and
shows the finished PDF path.

> **Scope and responsible use.** This tool is for making a personal offline copy of content you
> are already licensed to access (for example through your library). It does not log in, obtain
> tokens, or bundle any book content. You are responsible for complying with the provider's terms
> of service and the copyright law that applies to you. Do not redistribute downloaded material.

## Run

Use Python 3.11 or newer:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m playwright install chromium
python main.py
```

An installed application also provides the `book-dashboard` command. On Linux, if Chromium reports
missing system libraries, install them with `python -m playwright install-deps chromium`.

Use a terminal at least 80 columns wide and 24 rows high. Navigate with Tab / Shift+Tab and Enter,
or use the mouse. The settings pane scrolls. **Ctrl+S** starts a job; **Ctrl+Q** quits, waiting for any
active worker to acknowledge cancellation first.

Click **Paste token** or **Paste cookies** to read text copied from another application. The
credential fields also support **Ctrl+V**, **Ctrl+Shift+V**, and **Shift+Insert** when the terminal
forwards those keys. Terminal paste events are supported too. Pasting replaces the credential
field's contents; line breaks and surrounding whitespace in a token are removed.

The paste buttons use an existing desktop clipboard reader: `xclip` / `xsel` on X11, `wl-paste`
on Wayland, `pbpaste` on macOS, or PowerShell on Windows / WSL. Clipboard access happens only when
you select Paste. If no reader is available, use your terminal's Paste menu or install `xclip`
(`wl-clipboard` on Wayland). Clipboard content is kept in memory and is never logged.

## Settings and workflow

- **Host:** the viewer's HTTPS hostname, without the `/api/js/book/...` path. The previous
  hostname is remembered between runs.
- **Token:** paste the JWT from your current viewer session. It must contain `exp` and `data.docid`.
- **Cookie header:** paste the request's `Cookie` header, such as
  `ezproxy=...; ezproxyl=...; ezproxyn=...`. Leave it empty if the host does not need cookies.
- **First / last page:** the exact inclusive page range. Every requested page must pass validation;
  a missing page does not count as the end of the book.
- **Output directory:** the parent directory for new job folders, normally `jobs/`.

Advanced settings control the page request delay (default 0.3 seconds), concurrent asset downloads
(default 8), and TLS verification. For a university proxy with a private certificate chain, supply
its CA certificate file. If your existing setup requires disabled verification, explicitly uncheck
**Verify TLS certificates**; a supplied CA file takes precedence over that checkbox.

The dashboard saves host, range, output location, and advanced settings in `state/settings.json`.
It does not restore or save tokens and cookies. Credentials are masked in the form and redacted
from pipeline logs. JWT expiry and book identity are decoded locally for scheduling and checkpoint
matching; the server still decides whether the credentials are valid.

The workflow checks Chromium, downloads pages, discovers stylesheets and their assets, assembles
the pages, and prints with headless Chromium. Progress shows counts for downloads and an activity
indicator while rendering. A PDF is published only after required resources load successfully.

## Pause, cancel, and resume

When credentials expire or authentication fails, the job pauses. Paste a fresh token and Cookie
header, then select **Resume**. The replacement token must refer to the same book. The application
does not log in or obtain tokens for you.

To resume after restarting, select **Refresh jobs**, choose the unfinished job, enter credentials,
and select **Resume**. Selecting a job restores its nonsecret settings. Resume verifies downloaded
files with SHA-256 checksums, reuses intact files, and repairs missing or corrupted downloads.
Once pages and assets are complete, a rendering retry does not need network requests.

**Cancel** keeps completed downloads. Cancellation is cooperative: it waits for a current request
or blocking Chromium operation to finish. During printing, Chromium may take several minutes to
return. A new job cannot start while the previous worker is still active.

Each run has its own folder:

```text
jobs/<timestamp>-<id>/
  manifest.json     # Checkpoint and file integrity records
  pages/            # Downloaded page fragments
  assets/           # Local stylesheets, fonts, and other referenced resources
  render_pages/     # Page fragments with localized resource references
  combined.html
  quality-report.json  # Source image resolution measured in the print layout
  book.pdf          # Created only when rendering succeeds
```

Job directories are kept for recovery. Existing top-level `pages/`, `assets/`, `combined.html`, and
`book.pdf` are preserved. Legacy downloads are not automatically imported into resumable jobs
because their old manifests lack book identity and asset integrity records.

## Render existing local downloads

The original assembler entry point remains available for offline rendering:

```sh
python assemble_pdf.py
```

It reads the existing top-level pages and assets, validates the legacy page manifest when present,
and writes to a **new** `jobs/local-.../` folder. It never replaces the existing PDF. Alternate
directories can be supplied with `--pages`, `--assets`, and `--output`; `--output` must not exist yet.

The offline assembler requires complete local assets. Missing fonts that the original scripts
silently ignored now produce an explicit error identifying the missing file.

## PDF sharpness and source quality

Every real Chromium export writes `quality-report.json` alongside its PDF. The dashboard logs
a message when visible source images fall below 200 pixels per inch (PPI), including example
page numbers and the report path. This is advisory: low-resolution images still export; missing
or undecodable required resources still fail. The report measures the final print layout,
including image scaling and rotation, and identifies inline versus local images, vector images,
and pages with HTML text. HTML text presence does not mean that all prose is vector text.

Some viewer pages put prose inside JPEG images while keeping formulas as text. Printing those
pages cannot recover the detail already missing from the JPEGs. Sampled images in
some dashboard PDFs are approximately 108 PPI, with the original downloaded JPEG bytes
preserved. Increasing browser scale or re-downloading the same inline images does not create
detail. Genuine improvement requires a verified original PDF, higher-resolution image, or
vector alternative from the source. No such alternative has been verified for this viewer.

To inspect an existing dashboard download without printing another PDF or making network
requests, use its **render_pages** and **assets** directories:

```sh
python assemble_pdf.py \
  --pages jobs/<job-id>/render_pages \
  --assets jobs/<job-id>/assets \
  --inspect-only
```

This validates local fonts and images and creates a new job folder containing `combined.html`
and `quality-report.json`, without a PDF. Add `--output <new-directory>` to choose that folder.
Omit `--inspect-only` to render a fresh comparison PDF and report. Existing output directories
are rejected, preserving previous downloads and exports.

The 200 PPI threshold is a useful screening value, not a guarantee of quality. Measurements cover
visible HTML `img` elements; CSS background images and images inside SVGs are outside the report's
coverage. Vector images have no raster PPI. Unsupported 3D transforms are reported as unmeasured
rather than assigned a misleading value. The report contains no resource URLs, page text, or
credentials. It also explicitly records that better live sources and original physical page
dimensions have not been verified. Print dimensions remain those specified by the viewer's CSS;
they are not guessed from screen pixels. Existing job manifests remain compatible.

## Development and checks

```sh
python -m pip install -e '.[dev]'
python -m unittest discover -s tests -t .
ruff check book_dashboard tests main.py assemble_pdf.py
ruff format --check book_dashboard tests main.py assemble_pdf.py
```

The tests cover configuration, retries, authentication recovery, checkpoint integrity, TUI
interactions, and actual Chromium PDF page count, dimensions, and text. Browser tests require an
installed Chromium that can launch in the execution environment. Automated downloads use fake HTTP
responses; tests do not contact the book service.
Quality regression tests also cover JPEG byte preservation, vector text, print-layout PPI,
scaled and rotated images, vector and hidden images, report privacy, and offline inspection.

This version supports the existing viewer's HTML / CSS page layout. It fails explicitly on mixed
page dimensions or `srcset` images rather than producing a clipped or incomplete PDF. There is one
active job at a time, and page-count discovery is manual.

## Rebuild the PDF from OCR (selectable text and LaTeX math)

The dashboard PDF keeps each page's prose as a JPEG, so it cannot be sharpened. `rebuild_pdf.py`
instead recognizes the pages locally (Marker 1.10.1 with `force_ocr`, no LLM, no network after the
one-time model download) and typesets a new A4 PDF (11 pt, 20 mm margins, Latin Modern fonts,
French typography) with Pandoc and XeLaTeX. The source PDF is never modified. No paid API or
credential is involved.

### One-time setup

OCR dependencies live in their own Python 3.12 environment, separate from the main environment:

```sh
uv venv --python 3.12 .recon-env
uv pip install --python .recon-env/bin/python \
  --index-url https://download.pytorch.org/whl/cpu --extra-index-url https://pypi.org/simple \
  --index-strategy unsafe-best-match "marker-pdf==1.10.1"
```

`requirements-reconstruct.lock` records the exact resolved versions. Public model weights are
downloaded on first use (a few minutes) and cached. Pandoc, XeLaTeX and the Latin Modern fonts
must be installed (TeX Live). Processing is CPU-only with a single worker.

### Trial, review, recompile

```sh
# 1. Trial: pages 10 and 20 (inclusive, one-based PDF pages) into a NEW directory
python rebuild_pdf.py INPUT.pdf --output rebuild-trial

# 2. Review: open rebuild-trial/review.html (original | reconstruction | issues + every equation)
#    Correct text in rebuild-trial/pages/page_NNNN.md (the editable sources), then recompile
#    without running OCR again:
python rebuild_pdf.py INPUT.pdf --output rebuild-trial --resume --compile-only
```

`--pages` accepts lists and ranges (`10,20`, `5-8`). An existing output directory is rejected
unless `--resume` is given. Outputs: `rebuilt.pdf`, `book.md` (generated from `pages/`), `book.tex`,
`figures/`, `original/` (page images for review), `review.json`, `review.html`, `job.json`, and
`logs/` (OCR, Pandoc, XeLaTeX).

Corrections belong in `pages/page_NNNN.md`; they are never overwritten by OCR. `book.md` can be
edited directly for `--compile-only`, but a normal run refuses to regenerate over such edits.
`review.json` lists problems: pictures covering most of a page (prose not recognized, never
embedded as a screenshot), missing figure crops, empty pages, and suspicious formulas. Compiler
errors (invalid LaTeX, missing glyphs, missing figures) stop with a message naming the cause.

### Full book

The full book is unlocked only after the trial is accepted. Put a manually checked transcription of
each trial page at `rebuild-trial/reference/page_NNNN.txt`, check every equation in `review.html`,
then:

```sh
python rebuild_pdf.py --accept-trial --output rebuild-trial --equations-reviewed
python rebuild_pdf.py INPUT.pdf --output rebuild-trial --resume --all-pages
```

Acceptance requires at least 99% prose character accuracy per trial page, no blocking issues, no
unreviewed suspicious formulas, a successful compilation, and your confirmation of the equations.
Pages are processed sequentially. Each checkpoint is keyed by the input checksum, page, OCR
configuration and model versions, so an interrupted or repeated run skips finished pages and
keeps corrections. `review.json` and `job.json` record OCR time and peak memory.

Tests: `python -m unittest discover -s tests -t .` and `ruff check .`.

## Licenses

This project is MIT licensed (see `LICENSE`). The optional OCR environment installs third-party
tools under their own terms: `marker-pdf` and `surya-ocr` are GPL-3.0 and their model weights have
separate, restrictive licenses; Pandoc is GPL. They are installed and run separately and are not
bundled here. Check those terms before any commercial use.
