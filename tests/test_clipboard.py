import os
import subprocess
import unittest
from unittest.mock import Mock, patch

from book_dashboard.clipboard import ClipboardError, read_clipboard


class ClipboardTests(unittest.TestCase):
    def test_x11_reads_the_desktop_clipboard_not_primary_selection(self):
        result = subprocess.CompletedProcess([], 0, stdout="copied text", stderr="")
        with (
            patch.dict(os.environ, {"DISPLAY": ":test"}, clear=True),
            patch("book_dashboard.clipboard.sys.platform", "linux"),
            patch("book_dashboard.clipboard.shutil.which", return_value="/usr/bin/xclip"),
            patch("book_dashboard.clipboard.subprocess.run", return_value=result) as run,
        ):
            self.assertEqual(read_clipboard(), "copied text")
        self.assertEqual(run.call_args.args[0], ["xclip", "-selection", "clipboard", "-out"])
        self.assertEqual(run.call_args.kwargs["timeout"], 2)

    def test_wayland_precedes_x11_and_failed_reader_can_fall_back(self):
        success = subprocess.CompletedProcess([], 0, stdout="clipboard", stderr="")
        with (
            patch.dict(
                os.environ, {"WAYLAND_DISPLAY": "wayland-test", "DISPLAY": ":test"}, clear=True
            ),
            patch("book_dashboard.clipboard.sys.platform", "linux"),
            patch("book_dashboard.clipboard.shutil.which", return_value="reader"),
            patch(
                "book_dashboard.clipboard.subprocess.run", side_effect=[OSError(), success]
            ) as run,
        ):
            self.assertEqual(read_clipboard(), "clipboard")
        self.assertEqual(run.call_args_list[0].args[0], ["wl-paste", "--no-newline"])
        self.assertEqual(run.call_args_list[1].args[0][0], "xclip")

    def test_missing_tools_never_launch_a_process(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("book_dashboard.clipboard.sys.platform", "linux"),
            patch("book_dashboard.clipboard.subprocess.run") as run,
            self.assertRaisesRegex(ClipboardError, "No system clipboard reader"),
        ):
            read_clipboard()
        run.assert_not_called()

    def test_failed_reader_does_not_expose_output_or_error_contents(self):
        error = subprocess.CompletedProcess([], 1, stdout="private-token", stderr="private-cookie")
        run = Mock(side_effect=[subprocess.TimeoutExpired("xclip", 2), error])
        with (
            patch.dict(os.environ, {"DISPLAY": ":test"}, clear=True),
            patch("book_dashboard.clipboard.sys.platform", "linux"),
            patch("book_dashboard.clipboard.shutil.which", return_value="reader"),
            patch("book_dashboard.clipboard.subprocess.run", run),
            self.assertRaises(ClipboardError) as failure,
        ):
            read_clipboard()
        self.assertNotIn("private-token", str(failure.exception))
        self.assertNotIn("private-cookie", str(failure.exception))
