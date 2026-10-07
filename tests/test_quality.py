import base64
import contextlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import sync_playwright
from pypdf import PdfReader

from assemble_pdf import main as assemble_main
from book_dashboard.model import PipelineError
from book_dashboard.rendering import build_combined_html, render_pdf
from tests.helpers import CSS


class QualityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # A generated JPEG with raster prose, fine rules, and a diagram needs no Pillow
        # dependency and contains no copyrighted book material.
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                page = browser.new_page()
                cls.jpeg = page.evaluate("""() => {
                    const canvas = document.createElement('canvas');
                    canvas.width = 192; canvas.height = 96;
                    const ctx = canvas.getContext('2d');
                    ctx.fillStyle = 'white'; ctx.fillRect(0, 0, 192, 96);
                    ctx.fillStyle = 'black'; ctx.font = '12px serif';
                    ctx.fillText('Raster prose and fine lines', 4, 16);
                    ctx.fillRect(4, 24, 180, 1);
                    ctx.strokeRect(10, 40, 40, 40);
                    return canvas.toDataURL('image/jpeg', 0.9);
                }""")
            finally:
                browser.close()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.css = self.root / "style.css"
        self.css.write_text(CSS)
        self.events = []

    def inspect(self, content, *, inspect_only=True):
        source = self.root / "page_0010.html"
        source.write_text('<div class="pf w0 h0" data-page-no="a">' + content + "</div>")
        path, width, height = build_combined_html([source], [self.css], self.root / "combined.html")
        return render_pdf(
            path,
            width,
            height,
            self.root / "book.pdf",
            threading.Event(),
            self.events.append,
            inspect_only=inspect_only,
        )

    def test_jpeg_bytes_and_vector_formula_are_preserved_in_pdf(self):
        source_bytes = base64.b64decode(self.jpeg.partition(",")[2])
        report = self.inspect(
            f'<img src="{self.jpeg}" style="width:2in;height:1in">'
            '<div style="font:16pt serif">Vector formula: x + y = 2</div>',
            inspect_only=False,
        )
        reader = PdfReader(self.root / "book.pdf")
        self.assertEqual(len(reader.pages), 1)
        page = reader.pages[0]
        self.assertAlmostEqual(float(page.mediabox.width), 300, delta=1)
        self.assertAlmostEqual(float(page.mediabox.height), 450, delta=1)
        self.assertIn("Vector formula: x + y = 2", page.extract_text())
        images = [
            ref.get_object()
            for ref in page["/Resources"]["/XObject"].values()
            if ref.get_object().get("/Subtype") == "/Image"
        ]
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["/Filter"], "/DCTDecode")
        self.assertEqual((images[0]["/Width"], images[0]["/Height"]), (192, 96))
        self.assertEqual(images[0]._data, source_bytes)
        self.assertEqual(report["low_resolution_pages"], [10])
        self.assertTrue(report["pages"][0]["html_text_present"])
        self.assertTrue(any("below 200 PPI" in event.message for event in self.events))

    def test_print_geometry_controls_ppi_including_parent_scale_and_rotation(self):
        self.css.write_text(CSS + "@media print {img{width:1in!important;height:0.5in!important}}")
        report = self.inspect(
            f'<img src="{self.jpeg}" style="width:10px;height:5px">'
            '<div style="transform:scale(2);transform-origin:0 0">'
            f'<img src="{self.jpeg}" style="transform:rotate(37deg)"></div>'
        )
        first, second = report["pages"][0]["images"]
        self.assertAlmostEqual(first["ppi_x"], 192)
        self.assertAlmostEqual(first["ppi_y"], 192)
        self.assertAlmostEqual(second["ppi_x"], 96, delta=0.01)
        self.assertAlmostEqual(second["ppi_y"], 96, delta=0.01)
        self.assertFalse((self.root / "book.pdf").exists())
        self.assertEqual(report, json.loads((self.root / "quality-report.json").read_text()))

    def test_vectors_high_resolution_and_hidden_images_do_not_warn(self):
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="96" height="96">'
            '<path d="M0 0L96 96"/></svg>'
        )
        svg_url = "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()
        report = self.inspect(
            f'<img src="{self.jpeg}" style="width:0.5in;height:0.25in">'
            f'<img src="{svg_url}" style="width:2in;height:2in">'
            f'<img src="{self.jpeg}" style="display:none;width:3in;height:3in">'
        )
        images = report["pages"][0]["images"]
        self.assertEqual(len(images), 2)
        self.assertEqual(images[0]["ppi_x"], 384)
        self.assertEqual(images[1]["kind"], "vector")
        self.assertIsNone(images[1]["ppi_x"])
        self.assertEqual(report["low_resolution_pages"], [])
        self.assertFalse(any("below 200 PPI" in event.message for event in self.events))

    def test_object_fit_uses_actual_image_area_and_3d_is_unmeasured(self):
        report = self.inspect(
            f'<img src="{self.jpeg}" style="width:1in;height:1in;object-fit:contain">'
            f'<img src="{self.jpeg}" style="width:1in;height:1in;transform:rotateY(40deg)">'
        )
        first, second = report["pages"][0]["images"]
        self.assertEqual((first["ppi_x"], first["ppi_y"]), (192, 192))
        self.assertIsNone(second["ppi_x"])
        self.assertEqual(report["unmeasured_pages"], [10])

    def test_report_excludes_html_text_urls_and_credentials(self):
        secret = "session-secret-do-not-record"
        image = self.root / f"{secret}.jpg"
        image.write_bytes(base64.b64decode(self.jpeg.partition(",")[2]))
        self.inspect(
            f'<p>{secret}</p><img alt="{secret}" src="{image.as_uri()}?token={secret}" '
            'style="width:1in;height:0.5in">'
        )
        report_text = (self.root / "quality-report.json").read_text()
        self.assertNotIn(secret, report_text)
        self.assertNotIn("file:", report_text)
        self.assertNotIn(self.jpeg, report_text)

    def test_existing_pdf_and_report_are_preserved(self):
        (self.root / "book.pdf").write_bytes(b"existing PDF")
        (self.root / "quality-report.json").write_text("existing report")
        with self.assertRaisesRegex(PipelineError, "preserved"):
            self.inspect("<p>New content</p>", inspect_only=False)
        self.assertEqual((self.root / "book.pdf").read_bytes(), b"existing PDF")
        self.assertEqual((self.root / "quality-report.json").read_text(), "existing report")

    def test_offline_inspection_cli_writes_report_in_new_folder_without_pdf(self):
        pages = self.root / "pages"
        assets = self.root / "assets"
        output = self.root / "inspection"
        pages.mkdir()
        assets.mkdir()
        (pages / "page_0010.html").write_text(
            '<div class="pf w0 h0" data-page-no="a">'
            f'<img src="{self.jpeg}" style="width:2in;height:1in"></div>'
        )
        (assets / "style.css").write_text(CSS)
        args = [
            "assemble_pdf.py",
            "--pages",
            str(pages),
            "--assets",
            str(assets),
            "--output",
            str(output),
            "--inspect-only",
        ]
        stdout = io.StringIO()
        with patch("sys.argv", args), contextlib.redirect_stdout(stdout):
            self.assertEqual(assemble_main(), 0)
        self.assertTrue((output / "quality-report.json").is_file())
        self.assertFalse((output / "book.pdf").exists())
        self.assertIn("Quality report ready", stdout.getvalue())
        original_report = (output / "quality-report.json").read_bytes()
        with patch("sys.argv", args), contextlib.redirect_stdout(stdout):
            self.assertEqual(assemble_main(), 1)
        self.assertEqual((output / "quality-report.json").read_bytes(), original_report)
