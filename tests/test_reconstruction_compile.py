import shutil
import struct
import subprocess
import tempfile
import unittest
import zlib
from pathlib import Path

from book_dashboard.model import PipelineError
from book_dashboard.reconstruction.compile import compile_book, latex_problems, missing_figures


def make_png(size: int = 8) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    rows = b"".join(b"\x00" + b"\x80" * size for _ in range(size))
    header = struct.pack(">IIBBBBB", size, size, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


PNG = make_png()


def pdf_text(path: Path) -> str:
    return subprocess.run(
        ["pdftotext", str(path), "-"], capture_output=True, text=True, check=True
    ).stdout


@unittest.skipUnless(shutil.which("xelatex") and shutil.which("pandoc"), "needs Pandoc and XeLaTeX")
class CompileTests(unittest.TestCase):
    def build(self, markdown: str, *, figure=False) -> Path:
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        (directory / "book.md").write_text(markdown, encoding="utf-8")
        if figure:
            (directory / "figures").mkdir()
            (directory / "figures" / "a.png").write_bytes(PNG)
        return directory

    def test_french_text_and_math_become_selectable_text(self):
        directory = self.build(
            "## 1.2. Théorème\n\nSoit $a\\in\\mathbb{Z}$ où l’élève écrit « œuvre ».\n\n"
            "$$\\sum_{k=1}^{n} k = \\frac{n(n+1)}{2}$$\n\n![Schéma](figures/a.png)\n",
            figure=True,
        )
        pdf = compile_book(directory)
        text = pdf_text(pdf)
        for expected in ("Théorème", "élève", "œuvre"):
            self.assertIn(expected, text)
        self.assertNotIn("0.1", text)  # headings keep the book's own numbering
        self.assertTrue((directory / "book.tex").is_file())
        self.assertTrue((directory / "logs" / "xelatex.log").is_file())

    def test_invalid_latex_is_reported(self):
        directory = self.build("Voici $\\notacommand{x}$ ici.\n")
        with self.assertRaisesRegex(PipelineError, "LaTeX error"):
            compile_book(directory)

    def test_missing_figure_is_reported_before_compiling(self):
        directory = self.build("![x](figures/gone.png)\n")
        with self.assertRaisesRegex(PipelineError, "figures/gone.png"):
            compile_book(directory)
        self.assertFalse((directory / "rebuilt.pdf").exists())

    def test_missing_glyph_is_an_error(self):
        directory = self.build("Texte 😀 ici.\n")
        with self.assertRaisesRegex(PipelineError, "Missing glyphs"):
            compile_book(directory)

    def test_missing_source(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(PipelineError):
            compile_book(Path(tmp))


class HelperTests(unittest.TestCase):
    def test_missing_figures_ignores_urls_and_existing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ok.png").write_bytes(b"x")
            md = "![a](ok.png) ![b](nope.png) ![c](https://x/y.png)"
            self.assertEqual(missing_figures(md, root), ["nope.png"])

    def test_latex_problems(self):
        log = "! Undefined control sequence.\nMissing character: There is no ☃ in font x!"
        found = latex_problems(log)
        self.assertEqual(len(found), 2)
