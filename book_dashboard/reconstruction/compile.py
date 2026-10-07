from __future__ import annotations

import re
import shutil
import subprocess
from importlib import resources
from pathlib import Path

from book_dashboard.model import PipelineError

IMAGE_REFERENCE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)")
LATEX_ERROR = re.compile(r"^! (.+)$", re.MULTILINE)
MISSING_GLYPH = re.compile(r"Missing character: There is no (.+?) in font")
TIMEOUT_SECONDS = 600


def template_path() -> Path:
    return Path(str(resources.files("book_dashboard.reconstruction") / "templates" / "book.latex"))


def missing_figures(markdown: str, root: Path) -> list[str]:
    return sorted(
        {
            target
            for target in IMAGE_REFERENCE.findall(markdown)
            if not target.startswith(("http://", "https://")) and not (root / target).is_file()
        }
    )


def latex_problems(log: str) -> list[str]:
    """Actionable problems found in an XeLaTeX log: errors, missing glyphs, missing files."""
    problems = [f"LaTeX error: {message}" for message in LATEX_ERROR.findall(log)]
    glyphs = sorted(set(MISSING_GLYPH.findall(log)))
    if glyphs:
        problems.append(
            "Missing glyphs in Latin Modern: "
            + ", ".join(glyphs)
            + ". Replace them in book.md or map them to LaTeX commands."
        )
    return problems


def _run(command: list[str], cwd: Path, log: Path) -> subprocess.CompletedProcess:
    if shutil.which(command[0]) is None:
        raise PipelineError(f"{command[0]} is not installed or not on PATH.")
    try:
        completed = subprocess.run(
            command, cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired as error:
        raise PipelineError(f"{command[0]} timed out after {TIMEOUT_SECONDS} seconds.") from error
    log.write_text(completed.stdout + completed.stderr, encoding="utf-8")
    return completed


def compile_book(directory: Path, *, title: str | None = None) -> Path:
    """Compile ``book.md`` in *directory* into ``book.tex`` and ``rebuilt.pdf``."""
    source = directory / "book.md"
    if not source.is_file():
        raise PipelineError(f"{source} does not exist; run the OCR step first.")
    missing = missing_figures(source.read_text(encoding="utf-8"), directory)
    if missing:
        raise PipelineError("Missing figure files referenced by book.md: " + ", ".join(missing))
    logs = directory / "logs"
    logs.mkdir(exist_ok=True)
    pandoc = [
        "pandoc",
        "book.md",
        "--from=markdown+tex_math_dollars+raw_tex-yaml_metadata_block-smart",
        "--to=latex",
        f"--template={template_path()}",
        "--output=book.tex",
    ]
    if title:
        pandoc.append(f"--metadata=title:{title}")
    done = _run(pandoc, directory, logs / "pandoc.log")
    if done.returncode:
        raise PipelineError(f"Pandoc failed (see {logs / 'pandoc.log'}):\n{done.stderr.strip()}")
    xelatex = [
        "xelatex",
        "-interaction=nonstopmode",
        "-halt-on-error",
        "-jobname=rebuilt",
        "book.tex",
    ]
    done = _run(xelatex, directory, logs / "xelatex.log")
    raw_log = directory / "rebuilt.log"
    log = raw_log.read_text(encoding="utf-8", errors="replace") if raw_log.exists() else done.stdout
    if raw_log.exists():
        raw_log.replace(logs / "rebuilt.log")
    problems = latex_problems(log)
    if done.returncode or problems:
        raise PipelineError(
            "XeLaTeX reported problems (full log: "
            + str(logs / "rebuilt.log")
            + "):\n"
            + "\n".join(problems or ["XeLaTeX exited with an error."])
        )
    pdf = directory / "rebuilt.pdf"
    if not pdf.is_file():
        raise PipelineError("XeLaTeX finished without producing rebuilt.pdf.")
    return pdf
