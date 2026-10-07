"""Configuration, user-facing failures, and progress shared by the UI and pipeline."""

from __future__ import annotations

import base64
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_HOST = ""


class PipelineError(Exception):
    """A failure which should be displayed rather than hidden."""


class AuthenticationRequired(PipelineError):
    """The job can resume after the user supplies fresh credentials."""


class Cancelled(PipelineError):
    """The worker has acknowledged a cancellation request."""


def decode_token(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) != 3 or not all(parts):
            raise ValueError
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        expiry = payload["exp"]
        document_id = payload["data"]["docid"]
        if (
            isinstance(expiry, bool)
            or not isinstance(expiry, (float, int))
            or not math.isfinite(expiry)
            or expiry <= 0
            or not isinstance(document_id, (str, int))
            or isinstance(document_id, bool)
            or not str(document_id).strip()
        ):
            raise ValueError
        return payload
    except (ValueError, KeyError, TypeError, UnicodeError) as exc:
        raise PipelineError(
            "Enter a JWT with an expiry (exp) and a data.docid book identifier."
        ) from exc


def parse_cookies(header: str) -> dict[str, str]:
    header = header.strip()
    if header.lower().startswith("cookie:"):
        header = header[7:].strip()
    if "\n" in header or "\r" in header:
        raise PipelineError("Paste a single Cookie header, without line breaks.")
    cookies = {}
    for item in header.split(";"):
        if not item.strip():
            continue
        name, separator, value = item.strip().partition("=")
        if not separator or not re.fullmatch(r"[!#$%&'*+.^_`|~\w-]+", name, re.ASCII):
            raise PipelineError("Cookies must use name=value pairs separated by semicolons.")
        if name in cookies:
            raise PipelineError(f"The Cookie header repeats {name}.")
        cookies[name] = value
    return cookies


@dataclass(frozen=True)
class Config:
    host: str
    token: str = field(repr=False)
    cookie_header: str = field(default="", repr=False)
    first_page: int = 1
    last_page: int = 1
    output_dir: Path = Path("jobs")
    request_delay: float = 0.3
    asset_workers: int = 8
    verify_tls: bool = True
    ca_bundle: str = ""

    def __post_init__(self):
        host = self.host.strip()
        try:
            parsed = urlsplit(host if "://" in host else "https://" + host)
            port = parsed.port
        except ValueError as exc:
            raise PipelineError("The host or its port is invalid.") from exc
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or re.search(r"\s", parsed.netloc)
            or port == 0
        ):
            raise PipelineError("Enter an HTTPS hostname, without an API path or credentials.")
        if not 1 <= self.first_page <= self.last_page:
            raise PipelineError("The page range must satisfy 1 <= first page <= last page.")
        if not math.isfinite(self.request_delay) or not 0 <= self.request_delay <= 60:
            raise PipelineError("Request delay must be between 0 and 60 seconds.")
        if not 1 <= self.asset_workers <= 32:
            raise PipelineError("Asset workers must be between 1 and 32.")
        if self.ca_bundle and not Path(self.ca_bundle).expanduser().is_file():
            raise PipelineError("The CA certificate file does not exist.")
        object.__setattr__(self, "host", parsed.netloc.lower())
        object.__setattr__(self, "token", self.token.strip())
        object.__setattr__(self, "output_dir", Path(self.output_dir).expanduser().resolve())
        decode_token(self.token)
        parse_cookies(self.cookie_header)

    @property
    def expiry(self) -> float:
        # Decoding is only a local scheduling hint, never proof of authorization.
        return float(decode_token(self.token)["exp"])

    @property
    def identity(self) -> dict:
        return {
            "host": self.host,
            "document_id": str(decode_token(self.token)["data"]["docid"]),
            "first_page": self.first_page,
            "last_page": self.last_page,
        }

    @property
    def base_url(self) -> str:
        return f"https://{self.host}/api/js/book/{self.token}"

    def settings(self) -> dict:
        return {
            "host": self.host,
            "first_page": self.first_page,
            "last_page": self.last_page,
            "output_dir": str(self.output_dir),
            "request_delay": self.request_delay,
            "asset_workers": self.asset_workers,
            "verify_tls": self.verify_tls,
            "ca_bundle": self.ca_bundle,
        }

    def redact(self, message: str) -> str:
        message = message.replace(self.token, "<redacted>")
        for value in parse_cookies(self.cookie_header).values():
            if len(value) >= 4:
                message = message.replace(value, "<redacted>")
        return re.sub(r"eyJ[\w-]+\.[\w-]+\.[\w-]+", "<redacted>", message)


@dataclass(frozen=True)
class Progress:
    stage: str
    message: str
    completed: int = 0
    total: int | None = None
