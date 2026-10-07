import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from rebuild_pdf import main


class CliTests(unittest.TestCase):
    def run_cli(self, *arguments):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = main([str(a) for a in arguments])
        return code, stderr.getvalue()

    def test_missing_input_is_a_clean_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, error = self.run_cli(Path(tmp) / "none.pdf", "--output", Path(tmp) / "out")
        self.assertEqual(code, 1)
        self.assertIn("does not exist", error)

    def test_missing_reconstruction_environment_is_actionable(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "a.pdf"
            pdf.write_bytes(b"%PDF")
            code, error = self.run_cli(
                pdf, "--output", Path(tmp) / "out", "--python", Path(tmp) / "no-python"
            )
        self.assertEqual(code, 1)
        self.assertIn("reconstruction environment", error)

    def test_accept_trial_without_job_fails_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, error = self.run_cli("--accept-trial", "--output", Path(tmp) / "x")
        self.assertEqual(code, 1)
