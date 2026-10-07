import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from textual import events
from textual.widgets import Button, Input, RichLog, Select, Static

from book_dashboard.clipboard import ClipboardError
from book_dashboard.model import Progress
from book_dashboard.pipeline import Result
from book_dashboard.storage import Job
from book_dashboard.tui import Dashboard
from tests.helpers import token


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def wait_until(self, pilot, predicate):
        for _ in range(100):
            if predicate():
                return
            await pilot.pause(0.02)
        self.fail("Dashboard did not reach the expected state")

    def fill(self, app, root):
        for name, value in {
            "token": token(),
            "host": "reader.example",
            "cookies": "session=test-secret",
            "first-page": "1",
            "last-page": "2",
            "output-dir": str(root / "jobs"),
        }.items():
            app.query_one("#" + name, Input).value = value

    async def test_invalid_inputs_do_not_start_a_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Dashboard(
                settings_path=Path(directory) / "settings.json",
                runner=lambda *args: self.fail("Invalid form started a worker"),
            )
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.click("#start")
                await pilot.pause()
                self.assertFalse(app.active)
                self.assertIsNone(app.last_result)
                self.assertFalse(app.settings_path.exists())
                self.assertIn("numeric", str(app.query_one("#status", Static).render()))

    async def test_start_progress_cancel_and_duplicate_start_prevention(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []
            entered = threading.Event()

            def runner(configuration, emit, cancel, job_path):
                calls.append(configuration)
                emit(Progress("pages", "Downloading page 1", 1, 2))
                entered.set()
                cancel.wait(5)
                return Result("cancelled", error="Cancelled")

            app = Dashboard(settings_path=root / "settings.json", runner=runner)
            async with app.run_test(size=(100, 32)) as pilot:
                self.fill(app, root)
                try:
                    await pilot.click("#start")
                    await self.wait_until(pilot, entered.is_set)
                    self.assertTrue(app.active)
                    self.assertTrue(app.query_one("#start", Button).disabled)
                    self.assertIn(
                        "Downloading page 1", str(app.query_one("#status", Static).render())
                    )
                    app.launch()
                    self.assertEqual(len(calls), 1)
                    saved = app.settings_path.read_text()
                    self.assertNotIn(calls[0].token, saved)
                    self.assertNotIn("test-secret", saved)
                    self.assertTrue(app.query_one("#token", Input).password)
                    self.assertTrue(app.query_one("#cookies", Input).password)
                    await pilot.click("#cancel")
                    await self.wait_until(pilot, lambda: not app.active)
                    self.assertEqual(app.last_result.status, "cancelled")
                finally:
                    app.cancel_signal.set()

    async def test_authentication_pause_and_resume_with_replacement_token(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []

            def runner(configuration, emit, cancel, job_path):
                calls.append((configuration, job_path))
                job = Job.load(job_path, configuration) if job_path else Job.create(configuration)
                if len(calls) == 1:
                    job.status("paused")
                    return Result(
                        "paused", job.path, error="Token expired. Paste fresh credentials."
                    )
                output = job.path / "book.pdf"
                output.write_bytes(b"%PDF-test")
                job.status("completed")
                return Result("completed", job.path, output)

            app = Dashboard(settings_path=root / "settings.json", runner=runner)
            async with app.run_test(size=(100, 32)) as pilot:
                self.fill(app, root)
                await pilot.click("#start")
                await self.wait_until(pilot, lambda: app.last_result is not None)
                await pilot.pause()
                self.assertEqual(app.last_result.status, "paused")
                self.assertIn("Token expired", str(app.query_one("#status", Static).render()))
                self.assertFalse(app.query_one("#resume", Button).disabled)
                paused_path = app.last_result.job_path
                self.assertEqual(app.query_one("#job", Select).value, str(paused_path))
                replacement = token(signature="new-token")
                app.query_one("#token", Input).value = replacement
                await pilot.click("#resume")
                await self.wait_until(pilot, lambda: app.last_result is not None)
                self.assertEqual(app.last_result.status, "completed")
                self.assertEqual(calls[1][0].token, replacement)
                self.assertEqual(calls[1][1], paused_path)
                self.assertIn("PDF ready", str(app.query_one("#status", Static).render()))

    async def test_saved_settings_restore_but_credentials_do_not(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings.json"
            settings.write_text(
                json.dumps(
                    {
                        "host": "saved.example",
                        "last_page": 12,
                        "token": "must-not-restore",
                        "cookie_header": "secret",
                    }
                )
            )
            app = Dashboard(settings_path=settings)
            async with app.run_test(size=(80, 24)):
                self.assertEqual(app.query_one("#host", Input).value, "saved.example")
                self.assertEqual(app.query_one("#last-page", Input).value, "12")
                self.assertEqual(app.query_one("#token", Input).value, "")
                self.assertEqual(app.query_one("#cookies", Input).value, "")

    async def test_terminal_paste_accepts_a_long_token_with_leading_and_wrapped_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = Dashboard(settings_path=root / "settings.json")
            expected = token(signature="s" * 2000)
            wrapped = (
                "\n  "
                + "\n".join(expected[n : n + 80] for n in range(0, len(expected), 80))
                + " \n"
            )
            async with app.run_test(size=(100, 32)) as pilot:
                self.fill(app, root)
                field = app.query_one("#token", Input)
                field.focus()
                await pilot.pause()
                app.post_message(events.Paste(wrapped))
                await self.wait_until(pilot, lambda: field.value == expected)
                self.assertEqual(app.configuration().token, expected)
                self.assertTrue(field.password)
                logged = "".join(line.text for line in app.query_one("#log", RichLog).lines)
                self.assertNotIn(expected, logged)
                self.assertFalse(app.settings_path.exists())

    async def test_clipboard_keyboard_shortcuts_replace_the_token(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Dashboard(settings_path=Path(directory) / "settings.json")
            expected = token()
            async with app.run_test(size=(100, 32)) as pilot:
                field = app.query_one("#token", Input)
                field.focus()
                await pilot.pause()
                with patch(
                    "book_dashboard.tui.read_clipboard", return_value="\n" + expected + "\n"
                ) as read:
                    for shortcut in ("ctrl+v", "ctrl+shift+v", "shift+insert"):
                        field.value = "old-value"
                        await pilot.press(shortcut)
                        await self.wait_until(pilot, lambda: field.value == expected)
                self.assertEqual(read.call_count, 3)

    async def test_paste_buttons_fill_both_credential_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = Dashboard(settings_path=root / "settings.json")
            expected = token()
            cookies = "Cookie: ezproxy=test-secret; session=a=b"
            async with app.run_test(size=(100, 40)) as pilot:
                self.fill(app, root)
                with patch(
                    "book_dashboard.tui.read_clipboard",
                    side_effect=[expected, "\n" + cookies + "\n"],
                ):
                    await pilot.click("#paste-token")
                    await self.wait_until(pilot, lambda: app.value("token") == expected)
                    await pilot.click("#paste-cookies")
                    await self.wait_until(pilot, lambda: app.value("cookies") == cookies)
                configuration = app.configuration()
                self.assertEqual(configuration.token, expected)
                self.assertEqual(configuration.cookie_header, cookies)
                self.assertFalse(app.settings_path.exists())

    async def test_clipboard_failure_and_empty_clipboard_preserve_the_token(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Dashboard(settings_path=Path(directory) / "settings.json")
            expected = token()
            async with app.run_test(size=(100, 32)) as pilot:
                field = app.query_one("#token", Input)
                field.value = expected
                field.focus()
                await pilot.pause()
                with patch(
                    "book_dashboard.tui.read_clipboard",
                    side_effect=ClipboardError("Clipboard unavailable"),
                ):
                    await pilot.press("ctrl+v")
                    await pilot.pause()
                self.assertEqual(field.value, expected)
                self.assertIn(
                    "Clipboard unavailable", str(app.query_one("#status", Static).render())
                )
                with patch("book_dashboard.tui.read_clipboard", return_value="\n  \n"):
                    await pilot.press("ctrl+v")
                    await pilot.pause()
                self.assertEqual(field.value, expected)
                self.assertIn("Clipboard is empty", str(app.query_one("#status", Static).render()))
