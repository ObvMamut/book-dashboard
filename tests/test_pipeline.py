import tempfile
import threading
import unittest
from dataclasses import replace
from html.parser import HTMLParser
from pathlib import Path

from book_dashboard.model import AuthenticationRequired, Cancelled, PipelineError
from book_dashboard.pipeline import run
from book_dashboard.storage import Job, asset_name, list_jobs
from tests.helpers import CSS, FakeClient, config, fake_renderer, responses_for, token


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = config(Path(self.temporary.name))
        self.requests = []
        self.events = []
        self.responses = responses_for(self.config)

    def run_pipeline(self, configuration=None, job_path=None, renderer=fake_renderer, cancel=None):
        return run(
            configuration or self.config,
            self.events.append,
            cancel or threading.Event(),
            job_path,
            preflight=lambda: None,
            renderer=renderer,
            client_factory=lambda *args: FakeClient(*args, self.responses, self.requests),
        )

    def test_complete_pipeline_orders_pages_and_uses_unique_job_directories(self):
        result = self.run_pipeline()
        self.assertEqual(result.status, "completed", result.error)
        self.assertTrue(result.pdf_path.is_file())
        document = (result.job_path / "combined.html").read_text()
        self.assertLess(document.index("Page 1"), document.index("Page 2"))
        self.assertEqual(list_jobs(self.config.output_dir), [])
        again = self.run_pipeline()
        self.assertNotEqual(result.job_path, again.job_path)
        self.assertTrue(result.pdf_path.is_file())

    def test_auth_pause_and_resume_skips_verified_pages(self):
        page2 = f"{self.config.base_url}/page/2"
        self.responses[page2] = AuthenticationRequired("Session expired")
        paused = self.run_pipeline()
        self.assertEqual(paused.status, "paused")
        self.assertEqual(Job.load(paused.job_path).manifest["status"], "paused")
        refreshed = replace(self.config, token=token(signature="refreshed"))
        self.responses.update(responses_for(refreshed))
        resumed = self.run_pipeline(refreshed, paused.job_path)
        self.assertEqual(resumed.status, "completed", resumed.error)
        self.assertNotIn(f"{refreshed.base_url}/page/1", self.requests)
        self.assertEqual(resumed.job_path, paused.job_path)

    def test_wrong_book_cannot_resume_and_makes_no_requests(self):
        job = Job.create(self.config)
        wrong = replace(self.config, token=token(document_id="different"))
        result = self.run_pipeline(wrong, job.path)
        self.assertEqual(result.status, "failed")
        self.assertIn("same host", result.error)
        self.assertEqual(self.requests, [])
        self.assertEqual(Job.load(job.path).manifest["status"], "ready")

    def test_mismatched_or_missing_pages_fail_before_rendering(self):
        self.responses[f"{self.config.base_url}/page/2"] = '<div data-page-no="3">wrong</div>'
        result = self.run_pipeline()
        self.assertEqual(result.status, "failed")
        self.assertIn("mismatched", result.error)
        self.assertFalse((result.job_path / "book.pdf").exists())

    def test_imports_and_assets_with_matching_basenames_are_downloaded(self):
        self.responses["https://reader.example/styles/main.css"] = (
            '@import "nested.css"; ' + CSS + "@font-face{font-family:a;src:url(../fonts/f.woff)}"
            "@font-face{font-family:b;src:url(../other/f.woff)}"
        )
        self.responses["https://reader.example/styles/nested.css"] = "body{color:black}"
        self.responses["https://reader.example/fonts/f.woff"] = b"font-a"
        self.responses["https://reader.example/other/f.woff"] = b"font-b"
        result = self.run_pipeline()
        self.assertEqual(result.status, "completed", result.error)
        for url in (
            "https://reader.example/fonts/f.woff",
            "https://reader.example/other/f.woff",
            "https://reader.example/styles/nested.css",
        ):
            self.assertIn(url, self.requests)
            self.assertTrue(
                (result.job_path / "assets" / asset_name(url, self.config.token)).is_file()
            )

    def test_resume_after_render_failure_uses_cached_assets_without_network(self):
        def failed_render(*args):
            raise PipelineError("Simulated renderer failure")

        result = self.run_pipeline(renderer=failed_render)
        self.assertEqual(result.status, "failed")
        self.requests.clear()
        resumed = self.run_pipeline(job_path=result.job_path)
        self.assertEqual(resumed.status, "completed", resumed.error)
        self.assertEqual(self.requests, [])

    def test_page_background_urls_are_localized_without_breaking_attribute_quotes(self):
        page_url = f"{self.config.base_url}/page/1"
        self.responses[page_url] = self.responses[page_url].replace(
            'class="pf w0 h0"',
            'class="pf w0 h0" style="background:url(&quot;/images/bg.png&quot;)"',
        )
        self.responses["https://reader.example/images/bg.png"] = b"image"
        result = self.run_pipeline()
        self.assertEqual(result.status, "completed", result.error)

        class Styles(HTMLParser):
            def handle_starttag(self, tag, attrs):
                attributes = dict(attrs)
                if "pf" in attributes.get("class", "").split():
                    self.style = attributes["style"]

        parser = Styles()
        parser.feed((result.job_path / "render_pages" / "page_0001.html").read_text())
        filename = asset_name("https://reader.example/images/bg.png", self.config.token)
        self.assertEqual(parser.style, f'background:url("assets/{filename}")')

    def test_corrupt_page_is_downloaded_again_on_resume(self):
        result = self.run_pipeline(
            renderer=lambda *args: (_ for _ in ()).throw(PipelineError("stop"))
        )
        Job.load(result.job_path).page_path(1).write_text("corrupted")
        self.requests.clear()
        resumed = self.run_pipeline(job_path=result.job_path)
        self.assertEqual(resumed.status, "completed", resumed.error)
        self.assertIn(f"{self.config.base_url}/page/1", self.requests)
        self.assertNotIn(f"{self.config.base_url}/page/2", self.requests)

    def test_asset_auth_failure_pauses_without_success(self):
        self.responses["https://reader.example/styles/main.css"] = (
            CSS + "@font-face{font-family:a;src:url(../fonts/f.woff)}"
        )
        self.responses["https://reader.example/fonts/f.woff"] = AuthenticationRequired("expired")
        result = self.run_pipeline()
        self.assertEqual(result.status, "paused")
        self.assertFalse(Job.load(result.job_path).manifest["assets_complete"])
        self.assertFalse((result.job_path / "book.pdf").exists())

    def test_cancelled_renderer_is_not_marked_completed(self):
        def cancelled_render(*args):
            raise Cancelled("Cancelled")

        result = self.run_pipeline(renderer=cancelled_render)
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(Job.load(result.job_path).manifest["status"], "cancelled")

    def test_unexpected_error_is_visible_and_credentials_are_redacted(self):
        def broken_render(*args):
            raise RuntimeError("Failure at " + self.config.base_url)

        result = self.run_pipeline(renderer=broken_render)
        self.assertEqual(result.status, "failed")
        self.assertIn("Unexpected RuntimeError", result.error)
        self.assertNotIn(self.config.token, result.error)
        self.assertTrue(all(self.config.token not in event.message for event in self.events))
