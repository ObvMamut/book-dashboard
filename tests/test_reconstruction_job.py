import json
import tempfile
import unittest
from pathlib import Path

from book_dashboard.model import PipelineError
from book_dashboard.reconstruction.acceptance import accept_trial, prose_accuracy
from book_dashboard.reconstruction.job import JobOptions, read_json, run_job

TEXT = "Soit a un entier relatif et b un entier naturel non nul, alors il existe un couple."


class FakeBackend:
    def __init__(self, models=None):
        self.models = models or {"marker-pdf": "1.10.1"}
        self.ocr_calls = []

    def describe(self, pdf):
        return {"config": {"force_ocr": True}, "models": self.models, "page_count": 20}

    def run(self, pdf, pages, out):
        self.ocr_calls.append(list(pages))
        for page in pages:
            yield {
                "page": page,
                "markdown": f"{TEXT} Page {page} : $x_{page}$ éèà.",
                "images": {},
                "structure": [],
                "seconds": 2.0,
                "peak_rss_mb": 100 + page,
            }


class JobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, True))
        self.pdf = self.tmp / "in.pdf"
        self.pdf.write_bytes(b"%PDF-fake")
        self.output = self.tmp / "job"
        self.backend = FakeBackend()
        self.compiled = []

    def options(self, **kwargs):
        def compiler(directory, title=None):
            self.compiled.append(directory)
            return directory / "rebuilt.pdf"

        return JobOptions(
            pdf=self.pdf,
            output=self.output,
            backend=self.backend,
            compiler=compiler,
            log=lambda message: None,
            **kwargs,
        )

    def test_default_trial_pages_and_outputs(self):
        job = run_job(self.options())
        self.assertEqual(job["selected_pages"], [10, 20])
        for name in ("book.md", "review.json", "review.html", "job.json"):
            self.assertTrue((self.output / name).is_file(), name)
        self.assertEqual(self.backend.ocr_calls, [[10, 20]])
        self.assertIn("é", (self.output / "book.md").read_text(encoding="utf-8"))
        self.assertEqual(read_json(self.output / "review.json")["peak_rss_mb"], 120)
        self.assertEqual(self.pdf.read_bytes(), b"%PDF-fake")

    def test_existing_output_requires_resume(self):
        run_job(self.options())
        with self.assertRaisesRegex(PipelineError, "already exists"):
            run_job(self.options())
        with self.assertRaisesRegex(PipelineError, "does not exist"):
            run_job(JobOptions(self.pdf, self.tmp / "none", resume=True, backend=self.backend))

    def test_resume_reuses_checkpoints_and_ocrs_only_new_pages(self):
        run_job(self.options())
        run_job(self.options(resume=True, pages="10,20"))
        self.assertEqual(self.backend.ocr_calls, [[10, 20]])
        run_job(self.options(resume=True, pages="10-11,20"))
        self.assertEqual(self.backend.ocr_calls, [[10, 20], [11]])

    def test_model_change_invalidates_checkpoints(self):
        run_job(self.options())
        self.backend.models = {"marker-pdf": "9.9"}
        run_job(self.options(resume=True))
        self.assertEqual(self.backend.ocr_calls, [[10, 20], [10, 20]])

    def test_corrections_in_page_files_survive_resume(self):
        run_job(self.options())
        page = self.output / "pages" / "page_0010.md"
        page.write_text(page.read_text(encoding="utf-8") + "CORRIGÉ\n", encoding="utf-8")
        self.backend.models = {"marker-pdf": "9.9"}
        run_job(self.options(resume=True))
        self.assertIn("CORRIGÉ", page.read_text(encoding="utf-8"))
        self.assertIn("CORRIGÉ", (self.output / "book.md").read_text(encoding="utf-8"))

    def test_compile_only_skips_ocr_and_uses_edited_book(self):
        run_job(self.options())
        book = self.output / "book.md"
        book.write_text("Édité à la main.\n", encoding="utf-8")
        run_job(self.options(resume=True, compile_only=True))
        self.assertEqual(self.backend.ocr_calls, [[10, 20]])
        self.assertEqual(book.read_text(encoding="utf-8"), "Édité à la main.\n")
        self.assertEqual(len(self.compiled), 2)

    def test_compile_only_picks_up_page_file_edits(self):
        run_job(self.options())
        page = self.output / "pages" / "page_0020.md"
        page.write_text(page.read_text(encoding="utf-8") + "AJOUT\n", encoding="utf-8")
        run_job(self.options(resume=True, compile_only=True))
        self.assertIn("AJOUT", (self.output / "book.md").read_text(encoding="utf-8"))

    def test_regeneration_refuses_to_discard_book_edits(self):
        run_job(self.options())
        (self.output / "book.md").write_text("edit\n", encoding="utf-8")
        with self.assertRaisesRegex(PipelineError, "manual edits"):
            run_job(self.options(resume=True))
        self.assertEqual((self.output / "book.md").read_text(encoding="utf-8"), "edit\n")

    def test_compile_only_requires_resume(self):
        with self.assertRaisesRegex(PipelineError, "--resume"):
            run_job(self.options(compile_only=True))

    def test_other_input_pdf_is_rejected(self):
        run_job(self.options())
        self.pdf.write_bytes(b"%PDF-other")
        with self.assertRaisesRegex(PipelineError, "differs"):
            run_job(self.options(resume=True))

    def test_all_pages_needs_an_accepted_trial(self):
        with self.assertRaisesRegex(PipelineError, "trial"):
            run_job(self.options(all_pages=True))
        run_job(self.options())
        with self.assertRaisesRegex(PipelineError, "not been accepted"):
            run_job(self.options(resume=True, all_pages=True))

    def test_compile_failure_is_raised_and_recorded(self):
        def failing(directory, title=None):
            raise PipelineError("LaTeX error: boom")

        options = self.options()
        options.compiler = failing
        with self.assertRaisesRegex(PipelineError, "boom"):
            run_job(options)
        self.assertFalse(read_json(self.output / "job.json")["compiled"])


class AcceptanceTests(unittest.TestCase):
    def test_accuracy_measure(self):
        self.assertEqual(prose_accuracy("abcd", "abcd"), 1.0)
        self.assertAlmostEqual(prose_accuracy("abcdefghij", "abcdefghix"), 0.9)
        self.assertEqual(prose_accuracy("é", "e"), 0.0)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, True))
        pdf = self.tmp / "in.pdf"
        pdf.write_bytes(b"%PDF-fake")
        self.output = self.tmp / "job"
        self.reference = self.tmp / "ref"
        self.reference.mkdir()
        run_job(
            JobOptions(
                pdf,
                self.output,
                backend=FakeBackend(),
                compiler=lambda d, title=None: d,
                log=lambda m: None,
            )
        )

    def write_references(self, suffix=""):
        for page in (10, 20):
            text = (self.output / "pages" / f"page_{page:04d}.md").read_text(encoding="utf-8")
            (self.reference / f"page_{page:04d}.txt").write_text(text + suffix, encoding="utf-8")

    def test_accept_unlocks_full_book(self):
        self.write_references()
        record = accept_trial(self.output, self.reference, equations_reviewed=True)
        self.assertEqual(record["pages"], [10, 20])
        self.assertTrue((self.output / "trial-accepted.json").is_file())
        json.loads((self.output / "trial-accepted.json").read_text())

    def test_rejects_low_accuracy_missing_reference_and_unreviewed_equations(self):
        with self.assertRaisesRegex(PipelineError, "no manually checked"):
            accept_trial(self.output, self.reference, equations_reviewed=True)
        self.write_references(" " + "mot supplémentaire " * 10)
        with self.assertRaisesRegex(PipelineError, "< 99%"):
            accept_trial(self.output, self.reference, equations_reviewed=True)
        self.write_references()
        with self.assertRaisesRegex(PipelineError, "equations not confirmed"):
            accept_trial(self.output, self.reference, equations_reviewed=False)
