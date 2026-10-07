"""Authenticated requests and resumable page / asset downloads."""

from __future__ import annotations

import html
import math
import re
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests

from .model import AuthenticationRequired, Cancelled, Config, PipelineError, Progress, parse_cookies
from .storage import Job, asset_key, asset_name, atomic_write, file_record, intact

PAGE_NUMBER = re.compile(r"data-page-no\s*=\s*['\"]([0-9a-fA-F]+)['\"]")
CSS_URL = re.compile(r"url\(\s*(['\"]?)([^)'\"]+)\1\s*\)", re.IGNORECASE)
CSS_IMPORT = re.compile(
    r"@import\s+(?:url\(\s*['\"]?([^)'\"]+)['\"]?\s*\)|['\"]([^'\"]+)['\"])",
    re.IGNORECASE,
)
SOURCE_ATTRIBUTE = re.compile(r"\b(src|poster)\s*=\s*(['\"])(.*?)\2", re.IGNORECASE)
STYLE_ATTRIBUTE = re.compile(r"\bstyle\s*=\s*(['\"])(.*?)\1", re.IGNORECASE | re.DOTALL)


def check_cancel(cancel: threading.Event):
    if cancel.is_set():
        raise Cancelled("Cancelled. Completed downloads are available for resume.")


class HttpClient:
    """Each download thread owns its Session; none are shared across threads."""

    def __init__(self, config: Config, cancel: threading.Event, emit: Callable[[Progress], None]):
        self.config = config
        self.cancel = cancel
        self.emit = emit
        self.stopped = threading.Event()
        self.local = threading.local()
        self.sessions: list[requests.Session] = []
        self.lock = threading.Lock()

    def check(self):
        check_cancel(self.cancel)
        check_cancel(self.stopped)

    def wait(self, seconds: float):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            self.check()
            self.cancel.wait(min(0.1, max(0, until - time.monotonic())))
        self.check()

    def session(self) -> requests.Session:
        if not hasattr(self.local, "session"):
            session = requests.Session()
            session.headers.update(
                {
                    "User-Agent": (
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
                    ),
                    "Accept": "*/*",
                    "Accept-Language": "en-US,en;q=0.9",
                }
            )
            self.local.session = session
            with self.lock:
                self.sessions.append(session)
        return self.local.session

    def get(self, url: str, *, binary: bool = False) -> bytes | str:
        config = self.config
        verify = str(Path(config.ca_bundle).expanduser()) if config.ca_bundle else config.verify_tls
        same_host = urlsplit(url).netloc.lower() == config.host
        cookies = parse_cookies(config.cookie_header) if same_host else {}
        headers = {"Referer": config.base_url} if same_host else {}
        for attempt in range(3):
            self.check()
            if config.expiry - time.time() < 30:
                raise AuthenticationRequired("Token is expired or expires in under 30 seconds.")
            try:
                response = self.session().get(
                    url,
                    headers=headers,
                    cookies=cookies,
                    timeout=(10, 20),
                    verify=verify,
                    allow_redirects=False,
                )
            except requests.exceptions.SSLError as exc:
                raise PipelineError(
                    "TLS verification failed. Configure a CA file or the explicit insecure option."
                ) from exc
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                if attempt == 2:
                    raise PipelineError("Network request failed after three attempts.") from exc
                self.emit(Progress("retry", "Network error; retrying the request."))
                self.wait(2**attempt)
                continue
            with response:
                if response.status_code in (401, 403) or 300 <= response.status_code < 400:
                    raise AuthenticationRequired(
                        "Server rejected the session or redirected to login. "
                        "Refresh token and cookies."
                    )
                if response.status_code == 429 or 500 <= response.status_code < 600:
                    if attempt == 2:
                        raise PipelineError(f"HTTP {response.status_code} after three attempts.")
                    delay = 2**attempt
                    retry_after = response.headers.get("Retry-After", "")
                    if retry_after:
                        try:
                            delay = float(retry_after)
                        except ValueError:
                            try:
                                date = parsedate_to_datetime(retry_after)
                                delay = (date - datetime.now(UTC)).total_seconds()
                            except (ValueError, TypeError, OverflowError):
                                pass  # A malformed Retry-After uses the normal bounded backoff.
                    if not math.isfinite(delay):
                        delay = 2**attempt
                    self.emit(Progress("retry", f"HTTP {response.status_code}; retrying."))
                    self.wait(min(60, max(0, delay)))
                    continue
                if response.status_code != 200:
                    raise PipelineError(
                        f"HTTP {response.status_code}; the requested resource is missing."
                    )
                content = response.content if binary else response.text
                if not content or (not binary and not content.strip()):
                    raise PipelineError("Server returned an empty resource.")
                return content
        raise AssertionError("request attempts exhausted")

    def close(self):
        for session in self.sessions:
            session.close()


def download_pages(job: Job, config: Config, client: HttpClient, emit: Callable[[Progress], None]):
    total = config.last_page - config.first_page + 1
    for completed, number in enumerate(range(config.first_page, config.last_page + 1), 1):
        client.check()
        path = job.page_path(number)
        record = job.manifest["pages"].get(str(number))
        cached = intact(path, record)
        if not cached:
            text = client.get(f"{config.base_url}/page/{number}")
            match = PAGE_NUMBER.search(text)
            if match is None or int(match[1], 16) != number:
                raise PipelineError(
                    f"Page {number} has a missing or mismatched data-page-no. No PDF was produced."
                )
            # Store a neutral placeholder rather than a live token embedded in HTML links.
            atomic_write(path, text.replace(config.token, "{BOOK_TOKEN}"))
            job.manifest["pages"][str(number)] = file_record(path)
            job.manifest["assets_complete"] = False
            job.save()
        emit(
            Progress("pages", f"Page {number}: {'cached' if cached else 'saved'}", completed, total)
        )
        if not cached and number < config.last_page:
            client.wait(config.request_delay)


class ViewerParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[str] = []
        self.styles: list[str] = []
        self.in_style = False
        self.base: str | None = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "base" and self.base is None:
            self.base = attributes.get("href")
        if tag == "link" and "stylesheet" in attributes.get("rel", "").lower().split():
            if attributes.get("href"):
                self.links.append(attributes["href"])
        if tag == "style":
            self.styles.append("")
            self.in_style = True

    def handle_endtag(self, tag):
        if tag == "style":
            self.in_style = False

    def handle_data(self, data):
        if self.in_style:
            self.styles[-1] += data


def resolve_ref(reference: str, source: str, token: str) -> str | None:
    reference = html.unescape(reference.strip()).strip("'\"").replace("{BOOK_TOKEN}", token)
    if not reference or reference.startswith(("data:", "#")):
        return None
    resolved = urljoin(source, reference)
    if urlsplit(resolved).scheme not in ("https", "http"):
        raise PipelineError("A resource uses an unsupported URL scheme.")
    return resolved


def css_references(text: str) -> list[str]:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return [match[2].strip() for match in CSS_URL.finditer(text)]


def import_references(text: str) -> list[str]:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return [(match[1] or match[2]).strip() for match in CSS_IMPORT.finditer(text)]


def page_references(text: str) -> list[str]:
    if re.search(r"\bsrcset\s*=", text, re.IGNORECASE):
        raise PipelineError("This viewer uses srcset images, which this version does not support.")
    return [match[3] for match in SOURCE_ATTRIBUTE.finditer(text)] + css_references(
        html.unescape(text)
    )


def rewrite_css(text: str, source: str, token: str, *, prefix: str = "") -> str:
    def local(reference: str) -> str:
        url = resolve_ref(reference, source, token)
        if url is None:
            return reference
        fragment = urlsplit(url).fragment
        return prefix + asset_name(url, token) + ("#" + fragment if fragment else "")

    text = CSS_URL.sub(lambda m: f'url("{local(m[2].strip())}")', text)
    # Quoted @import without url() wasn't handled above.
    return re.sub(
        r"(@import\s+)(['\"])([^'\"]+)\2",
        lambda m: f'{m[1]}"{local(m[3])}"',
        text,
        flags=re.IGNORECASE,
    )


def assets_intact(job: Job, config: Config) -> bool:
    manifest = job.manifest
    if not manifest["assets_complete"] or not manifest["stylesheets"]:
        return False
    if not all(
        intact(job.asset_path(record["filename"]), record) for record in manifest["assets"].values()
    ):
        return False
    return all(
        intact(job.page_path(n, rendered=True), manifest["render_pages"].get(str(n)))
        for n in range(config.first_page, config.last_page + 1)
    )


def download_assets(job: Job, config: Config, client: HttpClient, emit: Callable[[Progress], None]):
    if assets_intact(job, config):
        total = len(job.manifest["assets"])
        emit(Progress("assets", "All assets are cached and verified.", total, total))
        return
    job.manifest["assets_complete"] = False
    job.save()
    emit(Progress("assets", "Discovering viewer stylesheets and assets."))
    viewer = ViewerParser()
    viewer.feed(client.get(config.base_url))
    source = urljoin(config.base_url, viewer.base) if viewer.base else config.base_url
    css_documents: dict[str, tuple[str, str, str]] = {}
    top_level = []

    def collect_css(url: str):
        key = asset_key(url, config.token)
        if key in css_documents:
            return
        text = client.get(url)
        css_documents[key] = (asset_name(url, config.token), url, text)
        for reference in import_references(text):
            imported = resolve_ref(reference, url, config.token)
            if imported:
                collect_css(imported)

    for reference in dict.fromkeys(viewer.links):
        url = resolve_ref(reference, source, config.token)
        if url:
            collect_css(url)
            top_level.append(asset_name(url, config.token))
    for index, text in enumerate(viewer.styles):
        name = f"inline_{index}.css"
        css_documents[name] = (name, source, text)
        top_level.append(name)
        for reference in import_references(text):
            imported = resolve_ref(reference, source, config.token)
            if imported:
                collect_css(imported)
    if not top_level:
        raise PipelineError("No viewer stylesheets were found. Check the host and credentials.")

    resources = {}
    for _, css_source, text in css_documents.values():
        for reference in css_references(text):
            url = resolve_ref(reference, css_source, config.token)
            if url and asset_key(url, config.token) not in css_documents:
                resources[asset_key(url, config.token)] = url
    pages = {}
    for number in range(config.first_page, config.last_page + 1):
        client.check()
        text = job.page_path(number).read_text(encoding="utf-8")
        pages[number] = text
        for reference in page_references(text):
            url = resolve_ref(reference, source, config.token)
            if url:
                resources[asset_key(url, config.token)] = url

    total = len(resources)
    completed = 0
    pending = []
    for key, url in resources.items():
        record = job.manifest["assets"].get(key)
        if record and intact(job.asset_path(record["filename"]), record):
            completed += 1
        else:
            pending.append((key, url))
    emit(Progress("assets", f"Resources verified: {completed}/{total}", completed, total))

    def download_one(key: str, url: str):
        client.check()
        filename = asset_name(url, config.token)
        path = job.asset_path(filename)
        atomic_write(path, client.get(url, binary=True))
        return key, {"filename": filename, **file_record(path)}

    pool = ThreadPoolExecutor(max_workers=config.asset_workers)
    futures = [pool.submit(download_one, key, url) for key, url in pending]
    last_checkpoint = time.monotonic()
    try:
        for future in as_completed(futures):
            client.check()
            key, record = future.result()
            job.manifest["assets"][key] = record
            completed += 1
            if time.monotonic() - last_checkpoint >= 1:
                job.save()
                last_checkpoint = time.monotonic()
            if completed % 25 == 0 or completed == total:
                emit(Progress("assets", f"Resources saved: {completed}/{total}", completed, total))
    finally:
        # Prevent queued requests from running after cancellation, auth failure, or a disk error.
        client.stopped.set()
        pool.shutdown(wait=True, cancel_futures=True)
        job.save()

    check_cancel(client.cancel)
    for key, (filename, css_source, text) in css_documents.items():
        check_cancel(client.cancel)
        path = job.asset_path(filename)
        atomic_write(path, rewrite_css(text, css_source, config.token))
        job.manifest["assets"][key] = {"filename": filename, **file_record(path)}
    for number, text in pages.items():
        check_cancel(client.cancel)

        def rewrite_attribute(match):
            url = resolve_ref(match[3], source, config.token)
            value = "assets/" + asset_name(url, config.token) if url else html.unescape(match[3])
            if url and urlsplit(url).fragment:
                value += "#" + urlsplit(url).fragment
            return f"{match[1]}={match[2]}{html.escape(value, quote=True)}{match[2]}"

        text = SOURCE_ATTRIBUTE.sub(rewrite_attribute, text)
        text = STYLE_ATTRIBUTE.sub(
            lambda match: (
                'style="'
                + html.escape(
                    rewrite_css(html.unescape(match[2]), source, config.token, prefix="assets/"),
                    quote=True,
                )
                + '"'
            ),
            text,
        )
        text = re.sub(
            r"(<style\b[^>]*>)(.*?)(</style\s*>)",
            lambda match: (
                match[1]
                + rewrite_css(
                    match[2],
                    source,
                    config.token,
                    prefix="assets/",
                )
                + match[3]
            ),
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        # Page fragments need no executable viewer scripts; render only their content.
        text = re.sub(r"<script\b[^>]*>.*?</script\s*>", "", text, flags=re.I | re.S)
        path = job.page_path(number, rendered=True)
        atomic_write(path, text)
        job.manifest["render_pages"][str(number)] = file_record(path)
    job.manifest["stylesheets"] = list(dict.fromkeys(top_level))
    job.manifest["assets_complete"] = True
    job.save()
    emit(Progress("assets", "All required assets saved.", total, total))
