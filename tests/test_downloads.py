import threading
import time
import unittest
from unittest.mock import Mock, patch

import requests

from book_dashboard.downloads import HttpClient, rewrite_css
from book_dashboard.model import AuthenticationRequired, Cancelled, Config, PipelineError
from book_dashboard.storage import asset_name
from tests.helpers import token


def response(status=200, content=b"ok", headers=None):
    result = requests.Response()
    result.status_code = status
    result._content = content
    result._content_consumed = True
    result.encoding = "utf-8"
    result.headers.update(headers or {})
    return result


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.config = Config("reader.example", token(), cookie_header="session=secret")
        self.cancel = threading.Event()
        self.events = []
        self.client = HttpClient(self.config, self.cancel, self.events.append)

    def test_retry_after_and_transient_errors(self):
        session = Mock()
        session.get.side_effect = [
            requests.exceptions.Timeout(),
            response(429, headers={"Retry-After": "7"}),
            response(content=b"success"),
        ]
        with (
            patch.object(self.client, "session", return_value=session),
            patch.object(self.client, "wait") as wait,
        ):
            self.assertEqual(self.client.get("https://reader.example/resource"), "success")
        self.assertEqual(session.get.call_count, 3)
        self.assertEqual([call.args[0] for call in wait.call_args_list], [1, 7])

    def test_persistent_failure_is_not_success(self):
        session = Mock()
        session.get.side_effect = [response(503), response(503), response(503)]
        with (
            patch.object(self.client, "session", return_value=session),
            patch.object(self.client, "wait"),
            self.assertRaisesRegex(PipelineError, "three attempts"),
        ):
            self.client.get("https://reader.example/resource")
        self.assertEqual(session.get.call_count, 3)

    def test_authentication_redirects_and_expiry_pause(self):
        for status in (401, 403, 302):
            with (
                self.subTest(status=status),
                patch.object(
                    self.client,
                    "session",
                    return_value=Mock(get=Mock(return_value=response(status))),
                ),
                self.assertRaises(AuthenticationRequired),
            ):
                self.client.get("https://reader.example/resource")
        self.client.config = Config("reader.example", token(expiry=time.time() - 1))
        with (
            patch.object(self.client, "session") as session,
            self.assertRaises(AuthenticationRequired),
        ):
            self.client.get("https://reader.example/resource")
        session.assert_not_called()

    def test_cancellation_prevents_requests(self):
        self.cancel.set()
        with patch.object(self.client, "session") as session, self.assertRaises(Cancelled):
            self.client.get("https://reader.example/resource")
        session.assert_not_called()

    def test_credentials_are_only_sent_to_the_viewer_host(self):
        session = Mock(get=Mock(return_value=response()))
        with patch.object(self.client, "session", return_value=session):
            self.client.get("https://cdn.example/font.woff")
        self.assertEqual(session.get.call_args.kwargs["cookies"], {})
        self.assertEqual(session.get.call_args.kwargs["headers"], {})
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])

    def test_css_relative_urls_are_resolved_against_the_file_not_a_fake_directory(self):
        source = "https://reader.example/styles/main.css"
        rewritten = rewrite_css(
            "@font-face { src: url('../fonts/f.woff') }", source, self.config.token
        )
        self.assertIn(
            asset_name("https://reader.example/fonts/f.woff", self.config.token), rewritten
        )
        self.assertNotIn("../fonts", rewritten)
        self.assertEqual(
            rewrite_css("a{background:url(data:image/png;base64,abc)}", source, self.config.token),
            'a{background:url("data:image/png;base64,abc")}',
        )
