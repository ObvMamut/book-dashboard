"""Read desktop clipboard text only in response to an explicit paste action."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys


class ClipboardError(Exception):
    """Clipboard access is unavailable; existing field contents must be preserved."""


def read_clipboard() -> str:
    commands = []
    if os.environ.get("WAYLAND_DISPLAY"):
        commands.append(["wl-paste", "--no-newline"])
    if os.environ.get("DISPLAY"):
        commands.extend(
            [
                ["xclip", "-selection", "clipboard", "-out"],
                ["xsel", "--clipboard", "--output"],
            ]
        )
    if sys.platform == "darwin":
        commands.append(["pbpaste"])
    if sys.platform == "win32" or os.environ.get("WSL_DISTRO_NAME"):
        commands.append(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Get-Clipboard -Raw",
            ]
        )
    available = [command for command in commands if shutil.which(command[0])]
    if not available:
        raise ClipboardError(
            "No system clipboard reader is available. Use the terminal's Paste menu, "
            "or install xclip (X11) / wl-clipboard (Wayland)."
        )
    for command in available:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired, UnicodeError):
            continue
        if result.returncode == 0:
            return result.stdout
    # Do not surface subprocess stderr: clipboard helpers can include clipboard content in errors.
    raise ClipboardError("Could not read the system clipboard. Try the terminal's Paste menu.")
