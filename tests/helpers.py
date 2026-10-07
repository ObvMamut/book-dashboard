from __future__ import annotations

import base64
import json
import threading
import time
from pathlib import Path

from book_dashboard.downloads import check_cancel
from book_dashboard.model import Config


def token(*, document_id="42", expiry=None, signature="test-signature") -> str:
    payload = {
        "exp": time.time() + 3600 if expiry is None else expiry,
        "data": {"docid": document_id},
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return "eyJhbGciOiJIUzI1NiJ9." + encoded + "." + signature


def config(root: Path, **kwargs) -> Config:
    return Config(
        host="reader.example",
        token=token(),
        last_page=2,
        output_dir=root,
        request_delay=0,
        **kwargs,
    )


CSS = """.pf { position: relative; overflow: hidden; }
.w0 { width: 400px; } .h0 { height: 600px; }
@media print { .w0 { width: 300pt; } .h0 { height: 450pt; } }
"""


def fragment(number: int) -> str:
    return (
        f'<div class="pf w0 h0" data-page-no="{number:x}">'
        f'<div style="font-size:12pt">Page {number}</div></div>'
    )


class FakeClient:
    def __init__(self, configuration, cancel, emit, responses, requests):
        self.config = configuration
        self.cancel = cancel
        self.emit = emit
        self.responses = responses
        self.requests = requests
        self.stopped = threading.Event()

    def check(self):
        check_cancel(self.cancel)
        check_cancel(self.stopped)

    def get(self, url, *, binary=False):
        self.check()
        self.requests.append(url)
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return response

    def wait(self, seconds):
        self.check()

    def close(self):
        pass


def responses_for(configuration: Config) -> dict:
    return {
        configuration.base_url: '<link href="/styles/main.css" rel="stylesheet">',
        "https://reader.example/styles/main.css": CSS,
        **{
            f"{configuration.base_url}/page/{n}": fragment(n)
            for n in range(configuration.first_page, configuration.last_page + 1)
        },
    }


def fake_renderer(path, width, height, output, cancel, emit):
    check_cancel(cancel)
    output.write_bytes(b"%PDF-test")
