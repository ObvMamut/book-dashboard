import tempfile
import threading
import unittest
from pathlib import Path

from pypdf import PdfReader

from book_dashboard.model import Cancelled, PipelineError
from book_dashboard.rendering import build_combined_html, detect_page_size, render_pdf
from tests.helpers import CSS, fragment


class RenderingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.css = self.root / "style.css"
        self.css.write_text(CSS)
        self.pages = []
        for number in (1, 2):
            path = self.root / f"page_{number:04d}.html"
            path.write_text(fragment(number))
            self.pages.append(path)

    def test_print_rules_take_precedence_over_screen_dimensions(self):
        self.assertEqual(detect_page_size(fragment(1), [self.css]), ("300pt", "450pt"))

    def test_actual_chromium_pdf_has_expected_page_count_size_and_text(self):
        path, width, height = build_combined_html(
            self.pages, [self.css], self.root / "combined.html"
        )
        output = self.root / "book.pdf"
        render_pdf(path, width, height, output, threading.Event(), lambda event: None)
        reader = PdfReader(output)
        self.assertEqual(len(reader.pages), 2)
        for number, page in enumerate(reader.pages, 1):
            self.assertAlmostEqual(float(page.mediabox.width), 300, delta=1)
            self.assertAlmostEqual(float(page.mediabox.height), 450, delta=1)
            self.assertIn(f"Page {number}", page.extract_text())
        self.assertFalse((self.root / ".book.partial.pdf").exists())

    def test_missing_font_prevents_pdf_publication(self):
        self.css.write_text(CSS + '@font-face{font-family:missing;src:url("missing.woff")}')
        path, width, height = build_combined_html(
            self.pages, [self.css], self.root / "combined.html"
        )
        output = self.root / "book.pdf"
        with self.assertRaisesRegex(PipelineError, "failed to load"):
            render_pdf(path, width, height, output, threading.Event(), lambda event: None)
        self.assertFalse(output.exists())
        self.assertFalse((self.root / ".book.partial.pdf").exists())

    def test_missing_image_prevents_pdf_publication(self):
        self.pages[0].write_text(
            fragment(1).replace("</div></div>", '</div><img src="missing.png"></div>')
        )
        path, width, height = build_combined_html(
            self.pages, [self.css], self.root / "combined.html"
        )
        with self.assertRaisesRegex(PipelineError, "failed to load"):
            render_pdf(
                path, width, height, self.root / "book.pdf", threading.Event(), lambda event: None
            )
        self.assertFalse((self.root / "book.pdf").exists())

    def test_cancelled_render_cleans_temporary_output(self):
        path, width, height = build_combined_html(
            self.pages, [self.css], self.root / "combined.html"
        )
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(Cancelled):
            render_pdf(path, width, height, self.root / "book.pdf", cancel, lambda event: None)
        self.assertFalse((self.root / "book.pdf").exists())

    def test_mixed_page_sizes_are_rejected(self):
        self.css.write_text(CSS + "@media print {.w1{width:500pt}}")
        self.pages[1].write_text(fragment(2).replace("pf w0 h0", "pf w1 h0"))
        with self.assertRaisesRegex(PipelineError, "mixed page"):
            build_combined_html(self.pages, [self.css], self.root / "combined.html")
