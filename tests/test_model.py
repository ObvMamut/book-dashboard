import json
import tempfile
import time
import unittest
from pathlib import Path

from book_dashboard.model import Config, PipelineError, decode_token, parse_cookies
from book_dashboard.storage import Job, asset_key, asset_name, intact
from tests.helpers import token


class ConfigurationTests(unittest.TestCase):
    def test_cookie_header_preserves_equals_and_accepts_header_prefix(self):
        self.assertEqual(
            parse_cookies("Cookie: ezproxy=a=b; session=c"), {"ezproxy": "a=b", "session": "c"}
        )
        self.assertEqual(parse_cookies(""), {})
        for value in ("bad", "name=1; name=2", "name=1\nOther: header"):
            with self.subTest(value=value), self.assertRaises(PipelineError):
                parse_cookies(value)

    def test_validation_and_expiry(self):
        configuration = Config("https://reader.example/", token(expiry=time.time() - 5))
        self.assertEqual(configuration.host, "reader.example")
        self.assertLess(configuration.expiry, time.time())
        for kwargs in (
            {"first_page": 0},
            {"first_page": 3, "last_page": 2},
            {"asset_workers": 0},
            {"request_delay": float("nan")},
            {"ca_bundle": "/missing/cert"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(PipelineError):
                Config("reader.example", token(), **kwargs)
        for host in (
            "http://reader.example",
            "reader.example/api",
            "https://a:b@reader.example",
            "reader.example:bad",
            "reader.example\n",
        ):
            if host.endswith("\n"):
                continue  # Surrounding paste whitespace is allowed.
            with self.subTest(host=host), self.assertRaises(PipelineError):
                Config(host, token())
        for value in ("", "a.b.c", "only.two"):
            with self.assertRaises(PipelineError):
                decode_token(value)

    def test_checkpoint_and_settings_never_contain_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            credential = token()
            configuration = Config(
                "reader.example",
                credential,
                cookie_header="session=secretcookie",
                output_dir=Path(directory),
            )
            job = Job.create(configuration)
            serialized = (job.path / "manifest.json").read_text()
            self.assertNotIn(credential, serialized)
            self.assertNotIn("secretcookie", serialized)
            self.assertNotIn(credential, repr(configuration))
            self.assertEqual(json.loads(serialized)["identity"]["document_id"], "42")
            self.assertEqual(
                configuration.redact(f"URL/{credential} cookie=secretcookie"),
                "URL/<redacted> cookie=<redacted>",
            )

    def test_url_identity_survives_token_refresh_without_filename_collisions(self):
        old, new = token(signature="old"), token(signature="new")
        self.assertEqual(
            asset_key(f"https://host/book/{old}/f.woff", old),
            asset_key(f"https://host/book/{new}/f.woff", new),
        )
        urls = [
            "https://host/a/font.woff",
            "https://host/b/font.woff",
            "https://host/a/font.woff?variant=1",
        ]
        self.assertEqual(len({asset_name(url, old) for url in urls}), 3)

    def test_corrupt_and_mismatched_checkpoints_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            configuration = Config("reader.example", token(), output_dir=Path(directory))
            job = Job.create(configuration)
            other = Config("reader.example", token(document_id="another"))
            with self.assertRaisesRegex(PipelineError, "same host"):
                Job.load(job.path, other)
            with self.assertRaises(PipelineError):
                job.asset_path("../escape")
            (job.path / "manifest.json").write_text("broken")
            with self.assertRaisesRegex(PipelineError, "corrupt"):
                Job.load(job.path)
            self.assertFalse(intact(job.path / "missing", None))
