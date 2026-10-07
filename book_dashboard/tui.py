"""Keyboard-friendly Textual dashboard; processing stays outside the UI thread."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Callable
from pathlib import Path

from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import (
    Button,
    Checkbox,
    Collapsible,
    Footer,
    Header,
    Input,
    Label,
    ProgressBar,
    RichLog,
    Select,
    Static,
)

from .clipboard import ClipboardError, read_clipboard
from .model import DEFAULT_HOST, Config, PipelineError, Progress, decode_token
from .pipeline import Result, run
from .storage import atomic_write, list_jobs

SETTING_FIELDS = {
    "host": "host",
    "first_page": "first-page",
    "last_page": "last-page",
    "output_dir": "output-dir",
    "request_delay": "delay",
    "asset_workers": "workers",
    "ca_bundle": "ca-bundle",
}


class PipelineProgress(Message):
    def __init__(self, event: Progress):
        super().__init__()
        self.event = event


class PipelineFinished(Message):
    def __init__(self, result: Result):
        super().__init__()
        self.result = result


class CredentialPasteStatus(Message):
    def __init__(self, text: str):
        super().__init__()
        self.text = text


class CredentialInput(Input):
    """Paste complete credentials from the desktop or terminal without losing wrapped lines."""

    BINDINGS = [
        Binding("ctrl+shift+v", "paste", "Paste", show=False),
        Binding("shift+insert", "paste", "Paste", show=False),
    ]

    def apply_paste(self, text: str):
        if self.is_disabled:
            return
        value = "".join(text.split()) if self.id == "token" else " ".join(text.splitlines()).strip()
        if not value:
            self.post_message(CredentialPasteStatus("Clipboard is empty; the field was preserved."))
            return
        self.value = value
        self.cursor_position = len(value)
        label = "Token" if self.id == "token" else "Cookie header"
        self.post_message(CredentialPasteStatus(f"{label} pasted ({len(value)} characters)."))

    def _on_paste(self, event: events.Paste):
        self.apply_paste(event.text)
        event.stop()
        event.prevent_default()

    async def action_paste(self):
        if self.is_disabled:
            return
        try:
            text = await asyncio.to_thread(read_clipboard)
        except ClipboardError as exc:
            if self.app.clipboard:
                self.apply_paste(self.app.clipboard)
            else:
                self.post_message(CredentialPasteStatus(str(exc)))
            return
        self.apply_paste(text)


class Dashboard(App):
    TITLE = "Book dashboard"
    SUB_TITLE = "Pages → assets → PDF"
    BINDINGS = [("ctrl+q", "quit", "Quit"), ("ctrl+s", "start", "Start")]
    CSS = """
    Screen { layout: vertical; }
    #body { height: 1fr; }
    #settings { width: 44; min-width: 32; padding: 0 1; border-right: solid $primary; }
    #monitor { width: 1fr; padding: 0 1; }
    Label { height: auto; margin-top: 1; }
    Input { margin-bottom: 0; }
    .paste-actions { height: 3; }
    .paste-actions Button { width: 1fr; }
    #status, #clock, #result { height: auto; margin: 1 0; }
    #progress { height: 1; }
    #log { height: 1fr; border: round $primary; }
    #actions { height: 3; }
    #actions Button { min-width: 10; margin-right: 1; }
    #job { margin: 0 1; }
    Collapsible { padding: 0; margin-top: 1; }
    """

    def __init__(self, *, settings_path: Path | None = None, runner: Callable = run):
        super().__init__()
        self.settings_path = settings_path or Path.cwd() / "state" / "settings.json"
        self.runner = runner
        self.active = False
        self.cancel_signal = threading.Event()
        self.started_at = 0.0
        self.elapsed = 0.0
        self.last_result: Result | None = None
        self.quit_pending = False
        self.settings_warning = ""
        self.saved = {}
        if self.settings_path.exists():
            try:
                self.saved = json.loads(self.settings_path.read_text(encoding="utf-8"))
                if not isinstance(self.saved, dict):
                    raise ValueError
            except (OSError, ValueError):
                self.saved = {}
                self.settings_warning = "Saved settings could not be read; using defaults."

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="body"):
            with VerticalScroll(id="settings"):
                yield Label("Host")
                yield Input(str(self.saved.get("host", DEFAULT_HOST)), id="host")
                yield Label("Token")
                yield CredentialInput(password=True, placeholder="Paste the viewer JWT", id="token")
                with Horizontal(classes="paste-actions"):
                    yield Button("Paste token", id="paste-token")
                yield Label("Cookie header (optional)")
                yield CredentialInput(
                    password=True, placeholder="name=value; name=value", id="cookies"
                )
                with Horizontal(classes="paste-actions"):
                    yield Button("Paste cookies", id="paste-cookies")
                yield Label("First page")
                yield Input(str(self.saved.get("first_page", 1)), type="integer", id="first-page")
                yield Label("Last page")
                yield Input(str(self.saved.get("last_page", "")), type="integer", id="last-page")
                yield Label("Output directory")
                yield Input(str(self.saved.get("output_dir", Path.cwd() / "jobs")), id="output-dir")
                with Collapsible(title="Advanced settings", collapsed=True):
                    yield Label("Delay between pages (seconds)")
                    yield Input(
                        str(self.saved.get("request_delay", 0.3)), type="number", id="delay"
                    )
                    yield Label("Parallel asset downloads")
                    yield Input(
                        str(self.saved.get("asset_workers", 8)), type="integer", id="workers"
                    )
                    yield Label("CA certificate file (optional)")
                    yield Input(str(self.saved.get("ca_bundle", "")), id="ca-bundle")
                    yield Checkbox(
                        "Verify TLS certificates",
                        value=self.saved.get("verify_tls", True),
                        id="tls",
                    )
            with Vertical(id="monitor"):
                yield Static("Ready — enter settings and select Start.", id="status", markup=False)
                yield Static("", id="clock", markup=False)
                yield ProgressBar(total=1, show_eta=False, id="progress")
                yield RichLog(wrap=True, markup=False, max_lines=1000, id="log")
                yield Static("", id="result", markup=False)
        yield Select([], prompt="Choose an unfinished job to resume", id="job")
        with Horizontal(id="actions"):
            yield Button("Start", variant="primary", id="start")
            yield Button("Resume", id="resume", disabled=True)
            yield Button("Cancel", variant="warning", id="cancel", disabled=True)
            yield Button("Refresh jobs", id="refresh")
        yield Footer()

    def on_mount(self):
        self.set_interval(1, self.update_clock)
        self.refresh_jobs()
        if self.settings_warning:
            self.query_one("#log", RichLog).write(self.settings_warning)

    def value(self, identifier: str) -> str:
        return self.query_one("#" + identifier, Input).value.strip()

    def configuration(self) -> Config:
        try:
            return Config(
                host=self.value("host"),
                token=self.value("token"),
                cookie_header=self.value("cookies"),
                first_page=int(self.value("first-page")),
                last_page=int(self.value("last-page")),
                output_dir=Path(self.value("output-dir")),
                request_delay=float(self.value("delay")),
                asset_workers=int(self.value("workers")),
                verify_tls=self.query_one("#tls", Checkbox).value,
                ca_bundle=self.value("ca-bundle"),
            )
        except ValueError as exc:
            raise PipelineError(
                "Enter numeric page numbers, request delay, and worker count."
            ) from exc

    def refresh_jobs(self, selected: Path | None = None):
        root = Path(self.value("output-dir")).expanduser()
        try:
            jobs = list_jobs(root)
        except OSError as exc:
            self.show_error(f"Could not read the output directory: {type(exc).__name__}")
            return
        self.jobs = {str(job.path): job for job in jobs}
        selector = self.query_one("#job", Select)
        selector.set_options(
            [
                (
                    f"{job.path.name} · book {job.manifest['identity'].get('document_id', '?')} · "
                    f"{job.manifest.get('status', 'unfinished')}",
                    str(job.path),
                )
                for job in jobs
            ]
        )
        if selected is not None and str(selected) in self.jobs:
            selector.value = str(selected)
        self.query_one("#resume", Button).disabled = self.active or selector.value is Select.BLANK

    @on(Select.Changed, "#job")
    def select_job(self, message: Select.Changed):
        self.query_one("#resume", Button).disabled = self.active or message.value is Select.BLANK
        if self.active or message.value is Select.BLANK:
            return
        job = self.jobs.get(str(message.value))
        if job is None:
            return
        settings = {**job.manifest["settings"], **job.manifest["identity"]}
        for name, identifier in SETTING_FIELDS.items():
            if name in settings:
                self.query_one("#" + identifier, Input).value = str(settings[name])
        self.query_one("#tls", Checkbox).value = settings.get("verify_tls", True)
        if self.last_result is None or self.last_result.job_path != job.path:
            self.query_one("#status", Static).update(
                "Job selected. Enter credentials and select Resume."
            )

    def show_error(self, message: str):
        self.query_one("#status", Static).update(message)
        self.query_one("#log", RichLog).write(message)

    def on_credential_paste_status(self, message: CredentialPasteStatus):
        if not self.active:
            self.query_one("#status", Static).update(message.text)
            self.query_one("#log", RichLog).write(message.text)

    @on(Button.Pressed, "#paste-token")
    @on(Button.Pressed, "#paste-cookies")
    async def paste_credentials(self, event: Button.Pressed):
        identifier = "token" if event.button.id == "paste-token" else "cookies"
        field = self.query_one("#" + identifier, CredentialInput)
        field.focus()
        await field.action_paste()

    def launch(self, job_path: Path | None = None):
        if self.active:
            return
        try:
            config = self.configuration()
            if not self.value("output-dir"):
                raise PipelineError("Enter an output directory.")
            atomic_write(self.settings_path, json.dumps(config.settings(), indent=2))
        except (PipelineError, OSError) as exc:
            self.show_error(str(exc))
            return
        self.active = True
        self.cancel_signal = threading.Event()
        self.started_at = time.monotonic()
        self.last_result = None
        self.query_one("#result", Static).update("")
        self.query_one("#status", Static).update("Starting…")
        self.query_one("#progress", ProgressBar).update(total=None, progress=0)
        for identifier in ("start", "resume", "refresh"):
            self.query_one("#" + identifier, Button).disabled = True
        self.query_one("#cancel", Button).disabled = False
        self.query_one("#settings").disabled = True
        self.query_one("#job", Select).disabled = True
        self.execute(config, job_path, self.cancel_signal)

    @work(thread=True, exclusive=True, exit_on_error=False)
    def execute(self, config: Config, job_path: Path | None, cancel: threading.Event):
        def emit(event: Progress):
            self.post_message(
                PipelineProgress(
                    Progress(
                        event.stage,
                        config.redact(event.message),
                        event.completed,
                        event.total,
                    )
                )
            )

        try:
            result = self.runner(config, emit, cancel, job_path)
        except Exception as exc:
            # Boundary safeguard: the UI stays alive and explicitly reports worker failure.
            result = Result(
                "failed",
                job_path,
                error=config.redact(
                    f"Unexpected {type(exc).__name__}: {exc}",
                ),
            )
        self.post_message(PipelineFinished(result))

    def on_pipeline_progress(self, message: PipelineProgress):
        event = message.event
        self.query_one("#log", RichLog).write(f"{event.stage}: {event.message}")
        if event.stage == "retry":
            return
        if not self.cancel_signal.is_set():
            self.query_one("#status", Static).update(
                f"{event.stage.capitalize()} — {event.message}"
            )
        self.query_one("#progress", ProgressBar).update(
            total=max(1, event.total) if event.total is not None else None,
            progress=event.completed,
        )

    def on_pipeline_finished(self, message: PipelineFinished):
        self.active = False
        self.elapsed = time.monotonic() - self.started_at
        self.last_result = result = message.result
        self.query_one("#settings").disabled = False
        self.query_one("#job", Select).disabled = False
        for identifier in ("start", "refresh"):
            self.query_one("#" + identifier, Button).disabled = False
        self.query_one("#cancel", Button).disabled = True
        self.refresh_jobs(result.job_path)
        if result.status == "completed" and result.pdf_path:
            self.query_one("#status", Static).update("Completed — PDF ready.")
            self.query_one("#result", Static).update(
                f"{result.pdf_path}\n{result.pdf_path.stat().st_size / (1024 * 1024):.2f} MB",
            )
        else:
            self.query_one("#status", Static).update(
                f"{result.status.capitalize()} — {result.error}"
            )
        if self.quit_pending:
            self.exit()

    def update_clock(self):
        elapsed = time.monotonic() - self.started_at if self.active else self.elapsed
        remaining = "Enter a token to see its expiry"
        try:
            seconds = int(float(decode_token(self.value("token"))["exp"]) - time.time())
            remaining = f"Token expires in {seconds}s" if seconds > 0 else "Token expired"
        except PipelineError:
            pass  # An incomplete input is normal while the user pastes credentials.
        self.query_one("#clock", Static).update(f"Elapsed: {int(elapsed)}s · {remaining}")

    @on(Button.Pressed, "#start")
    def action_start(self):
        self.launch()

    @on(Button.Pressed, "#resume")
    def resume_job(self):
        selected = self.query_one("#job", Select).value
        if selected is not Select.BLANK:
            self.launch(Path(str(selected)))

    @on(Button.Pressed, "#refresh")
    def refresh_pressed(self):
        self.refresh_jobs()

    @on(Button.Pressed, "#cancel")
    def cancel_job(self):
        if self.active:
            self.cancel_signal.set()
            self.query_one("#cancel", Button).disabled = True
            self.query_one("#status", Static).update(
                "Cancellation pending — waiting for the current operation to finish.",
            )

    def action_quit(self):
        if self.active:
            self.quit_pending = True
            self.cancel_job()
        else:
            self.exit()


def main():
    Dashboard().run()
