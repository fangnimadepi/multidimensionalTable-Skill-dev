"""Preserve provider failures without disclosing upstream response text."""

from io import BytesIO
import json
from pathlib import Path
import ssl
import sys
import unittest
import urllib.error
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / "project-report"
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "runtime/scripts")]
from model_setup import ModelSetupError, verify_connection


class ModelConnectionTests(unittest.TestCase):
    options = {"provider": "deepseek", "text_model": "deepseek-flash", "api_base": "https://example.invalid"}

    def test_http_categories_survive_the_client_wrapper_without_leaking_bodies(self):
        for status, category in ((401, "authentication"), (402, "balance"), (403, "forbidden"),
                                 (429, "rate_limit"), (503, "server")):
            with self.subTest(status=status):
                failure = urllib.error.HTTPError("https://example.invalid", status, "untrusted-private-value", {}, BytesIO(b"private"))
                with patch("urllib.request.urlopen", side_effect=failure) as request, self.assertRaises(ModelSetupError) as caught:
                    verify_connection(self.options, "synthetic-key")
                self.assertEqual(caught.exception.details()["error_code"], "model_" + category)
                self.assertEqual(caught.exception.details()["http_status"], status)
                self.assertEqual(caught.exception.retryable, status in (429, 503))
                self.assertNotIn("private", str(caught.exception))
                self.assertNotIn("synthetic-key", str(caught.exception.details()))
                request.assert_called_once()

    def test_tls_is_actionable_and_does_not_trigger_network_retries(self):
        failure = urllib.error.URLError(ssl.SSLCertVerificationError("synthetic TLS problem"))
        with patch("urllib.request.urlopen", side_effect=failure) as request, self.assertRaises(ModelSetupError) as caught:
            verify_connection(self.options, "synthetic-key")
        self.assertEqual(caught.exception.code, "model_tls")
        self.assertFalse(caught.exception.retryable)
        request.assert_called_once()

    def test_format_only_retry_is_bounded_and_keeps_the_same_model(self):
        def response(content):
            return BytesIO(json.dumps({"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}).encode())
        with (patch("urllib.request.urlopen", side_effect=[response('{"ok":'), response('{"ok":true}')]) as request,
              patch("llm_client.time.sleep")):
            verify_connection(self.options, "synthetic-key")
        self.assertEqual(request.call_count, 2)
        payloads = [json.loads(call.args[0].data) for call in request.call_args_list]
        self.assertEqual({value["model"] for value in payloads}, {"deepseek-flash"})
        self.assertTrue(all(value["max_tokens"] == 128 for value in payloads))


if __name__ == "__main__":
    unittest.main()
