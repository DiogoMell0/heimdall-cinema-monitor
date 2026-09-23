"""Coleta HTTP e comandos de consulta."""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import date, datetime
from email.message import Message
from http.client import IncompleteRead
from io import BytesIO, StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.request import BaseHandler, build_opener
from urllib.response import addinfourl

from heimdall.cli import main
from heimdall.config import load_profile
from heimdall.models import ValidationError
from heimdall.service import analyze
from heimdall.sources.http import CollectionError, _NoRedirect, collect
from heimdall.sources.ingresso import sessions_url
from heimdall.sources.snapshot import load_snapshot, save_envelope
from tests.test_rules import PROFILE_PATH, ROOT, synthetic_payload

NOW = datetime.fromisoformat("2026-09-14T15:00:00-03:00")


class FakeResponse(BytesIO):
    def __init__(self, body=b"[]", *, status=200, headers=None, url=None):
        super().__init__(body)
        self.status = status
        self.headers = Message()
        for key, value in (headers if headers is not None else {"Content-Type": "application/json"}).items():
            self.headers[key] = value
        self.url = url

    def geturl(self):
        return self.url


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profile = load_profile(PROFILE_PATH)
        cls.url = sessions_url(cls.profile)

    def response(self, payload=None, **kwargs):
        body = json.dumps(synthetic_payload() if payload is None else payload).encode("utf-8")
        return FakeResponse(body, url=self.url, **kwargs)

    def collect_response(self, response, **kwargs):
        client = Mock()
        client.open.return_value = response
        with patch("heimdall.sources.http.datetime") as clock:
            clock.now.return_value = NOW
            result = collect(self.profile, opener=client, **kwargs)
        self.assertEqual(client.open.call_count, 1)
        return result, client

    def test_get_once_uses_correct_profile_and_real_collection_time(self):
        result, client = self.collect_response(self.response())
        request = client.open.call_args.args[0]
        self.assertEqual(request.full_url, self.url)
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertIsNone(request.get_header("Authorization"))
        self.assertIsNone(request.get_header("Cookie"))
        self.assertNotIn("partnership", request.full_url)
        self.assertEqual(client.open.call_args.kwargs["timeout"], 20)
        self.assertEqual(result.snapshot.captured_at, NOW)
        self.assertEqual(len(analyze(result.snapshot, self.profile).matching), 1)

    def test_valid_empty_json_and_204_leave_all_dates_pending(self):
        for response in [self.response([]), FakeResponse(b"", status=204, headers={}, url=self.url)]:
            with self.subTest(status=response.status):
                result, _ = self.collect_response(response)
                self.assertEqual(result.snapshot.sessions, ())
                self.assertEqual(len(analyze(result.snapshot, self.profile).pending_dates), 8)

    def test_204_with_content_is_inconclusive(self):
        with self.assertRaises(CollectionError):
            self.collect_response(FakeResponse(b"[]", status=204, url=self.url))

    def test_http_failures_never_become_empty_success_or_retry(self):
        for status, category in [(401, "acesso_recusado"), (403, "acesso_recusado"),
                                 (404, "nao_encontrado"), (429, "limite_de_acesso"),
                                 (500, "http"), (503, "http"), (206, "http")]:
            with self.subTest(status=status):
                headers = Message()
                headers["Retry-After"] = "120"
                client = Mock()
                error_body = BytesIO(b"not used as sessions")
                client.open.side_effect = HTTPError(self.url, status, "Erro", headers, error_body)
                with self.assertRaises(CollectionError) as raised:
                    collect(self.profile, opener=client)
                self.assertEqual(raised.exception.category, category)
                self.assertEqual(raised.exception.status, status)
                self.assertEqual(client.open.call_count, 1)
                self.assertTrue(error_body.closed)
                if status == 429:
                    self.assertIn("120 segundos", str(raised.exception))

    def test_retry_after_http_date_is_reported(self):
        headers = {"Retry-After": "Mon, 14 Sep 2026 20:00:00 GMT"}
        with self.assertRaisesRegex(CollectionError, "2026-09-14T20:00:00"):
            self.collect_response(FakeResponse(status=429, headers=headers, url=self.url))

    def test_redirect_is_rejected_by_real_urllib_handler_chain(self):
        calls = []

        class RedirectResponse(BaseHandler):
            handler_order = 0

            def https_open(inner_self, request):
                calls.append(request.full_url)
                headers = Message()
                headers["Location"] = "https://example.com/unexpected"
                response = addinfourl(BytesIO(b""), headers, request.full_url, 302)
                response.msg = "Found"
                return response

        client = build_opener(_NoRedirect(), RedirectResponse())
        with self.assertRaises(CollectionError) as raised:
            collect(self.profile, opener=client)
        self.assertEqual(raised.exception.category, "redirecionamento")
        self.assertEqual(calls, [self.url])

    def test_network_errors_and_timeouts_are_inconclusive(self):
        for failure, category in [(URLError("DNS failure"), "rede"),
                                  (URLError(TimeoutError()), "timeout"),
                                  (TimeoutError(), "timeout"), (ConnectionResetError(), "rede")]:
            with self.subTest(error=type(failure).__name__):
                client = Mock()
                client.open.side_effect = failure
                with self.assertRaises(CollectionError) as raised:
                    collect(self.profile, opener=client)
                self.assertEqual(raised.exception.category, category)
                self.assertEqual(client.open.call_count, 1)

    def test_timeout_or_partial_body_during_read_is_inconclusive(self):
        for failure in (TimeoutError(), IncompleteRead(b"[", 100)):
            response = self.response()
            response.read = Mock(side_effect=failure)
            with self.subTest(error=type(failure).__name__), self.assertRaises(CollectionError):
                self.collect_response(response)
            self.assertTrue(response.closed)

    def test_html_missing_mime_empty_body_invalid_json_and_wrong_schema(self):
        cases = [
            FakeResponse(b"<html>erro</html>", headers={"Content-Type": "text/html"}, url=self.url),
            FakeResponse(b"[]", headers={}, url=self.url),
            FakeResponse(b"", url=self.url), FakeResponse(b"[{", url=self.url),
            FakeResponse(b"\xff", url=self.url), self.response({"unexpected": []}),
            self.response([{"date": "2026-09-24"}]),
        ]
        for response in cases:
            with self.subTest(body=response.getvalue()), self.assertRaises(CollectionError):
                self.collect_response(response)

    def test_body_limit_with_and_without_declared_length(self):
        for headers in [{"Content-Type": "application/json"},
                        {"Content-Type": "application/json", "Content-Length": "100"}]:
            response = FakeResponse(b" " * 100, headers=headers, url=self.url)
            response.read = Mock(wraps=response.read)
            with self.subTest(headers=headers), self.assertRaises(CollectionError) as raised:
                self.collect_response(response, max_bytes=10)
            self.assertEqual(raised.exception.category, "resposta_grande")
            if "Content-Length" in headers:
                response.read.assert_not_called()
            else:
                response.read.assert_called_once_with(11)

    def test_incorrect_content_length_and_unsupported_encoding_fail(self):
        for extra in [{"Content-Length": "50"}, {"Content-Length": "-1"},
                      {"Content-Length": "oops"}, {"Content-Encoding": "gzip"}]:
            response = FakeResponse(b"[]", headers={"Content-Type": "application/json", **extra}, url=self.url)
            with self.subTest(extra=extra), self.assertRaises(CollectionError):
                self.collect_response(response)

    def test_unexpected_final_url_is_rejected(self):
        with self.assertRaises(CollectionError):
            self.collect_response(FakeResponse(url="https://example.com/"))

    def test_invalid_timeout_and_identity_do_not_make_requests(self):
        client = Mock()
        for timeout in (0, -1, 61, float("inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(ValidationError):
                collect(self.profile, timeout=timeout, opener=client)
        for city_id in ("../14", "14?date=2026-09-24", "14/other"):
            with self.subTest(city=city_id), self.assertRaises(ValidationError):
                collect(replace(self.profile, city_id=city_id), opener=client)
        client.open.assert_not_called()

    def test_online_new_date_is_discovered_and_old_sessions_rejected(self):
        payload = synthetic_payload()
        payload[0]["date"] = "2026-10-01"
        item = payload[0]["theaters"][0]["rooms"][0]["sessions"][0]
        item["realDate"]["localDate"] = item["date"]["localDate"] = "2026-10-01T20:00:00-03:00"
        result, _ = self.collect_response(self.response(payload))
        self.assertNotIn(date(2026, 10, 1), analyze(result.snapshot, self.profile).pending_dates)
        self.assertEqual(len(analyze(result.snapshot, self.profile).matching), 1)
        item["realDate"]["localDate"] = "2026-09-13T20:00:00-03:00"
        result, _ = self.collect_response(self.response(payload))
        self.assertEqual(analyze(result.snapshot, self.profile).matching, ())

    def test_save_and_replay_200_and_204_preserves_results(self):
        for response in [self.response(), FakeResponse(b"", status=204, headers={}, url=self.url)]:
            with self.subTest(status=response.status), tempfile.TemporaryDirectory() as folder:
                result, _ = self.collect_response(response)
                path = Path(folder) / "capture.json"
                save_envelope(path, result.envelope)
                self.assertEqual(load_snapshot(path, self.profile), result.snapshot)
                original = path.read_bytes()
                with self.assertRaises(FileExistsError):
                    save_envelope(path, result.envelope)
                self.assertEqual(path.read_bytes(), original)

    def test_cli_online_success_saves_and_labels_result(self):
        client = Mock()
        client.open.return_value = self.response()
        output = StringIO()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "capture.json"
            with patch("heimdall.sources.http.build_opener", return_value=client), \
                    patch("heimdall.sources.http.datetime") as clock, redirect_stdout(output):
                clock.now.return_value = NOW
                code = main(["consultar", "--salvar", str(path)])
            self.assertTrue(path.is_file())
            self.assertEqual(load_snapshot(path, self.profile).captured_at, NOW)
        self.assertEqual(code, 0)
        self.assertIn("Consulta ONLINE", output.getvalue())
        self.assertIn("COMPATÍVEL", output.getvalue())
        self.assertIn("Captura salva", output.getvalue())
        self.assertEqual(client.open.call_count, 1)

    def test_cli_failure_does_not_fallback_or_save_or_print_zero(self):
        output, error = StringIO(), StringIO()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "capture.json"
            with patch("heimdall.cli.collect", side_effect=CollectionError("timeout", "Tempo esgotado")), \
                    patch("heimdall.cli.load_snapshot") as offline, redirect_stdout(output), redirect_stderr(error):
                code = main(["consultar", "--salvar", str(path)])
            self.assertFalse(path.exists())
            offline.assert_not_called()
        self.assertEqual(code, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("inconclusivo", error.getvalue())

    def test_cli_refuses_existing_output_before_query(self):
        with patch("heimdall.cli.collect") as online, redirect_stderr(StringIO()):
            code = main(["consultar", "--salvar", str(PROFILE_PATH)])
        self.assertEqual(code, 1)
        online.assert_not_called()

    def test_fixture_preserves_visible_labels_and_all_sessions(self):
        evidence = ROOT / "tests/fixtures"
        capture_path = evidence / "programacao-atualizada.json"
        snapshot = load_snapshot(capture_path, self.profile)
        raw = json.loads(capture_path.read_text(encoding="utf-8"))
        site = json.loads((evidence / "rotulos-esperados.json").read_text(encoding="utf-8"))
        visible_labels = {
            session["id"]: {label["name"].casefold() for label in session["types"] if label["display"]}
            for day in raw["data"] for theater in day["theaters"]
            for room in theater["rooms"] for session in room["sessions"]
        }
        rows = {session.id: session for session in snapshot.sessions
                if session.programming_date.isoformat() == site["date"]}
        site_ids = {row[0] for cinema in site["cinemas"] for row in cinema["rows"]}
        self.assertEqual(set(rows), site_ids)
        self.assertEqual(len(rows), 21)
        for cinema in site["cinemas"]:
            for session_id, time, labels in cinema["rows"]:
                with self.subTest(session=session_id):
                    session = rows[session_id]
                    self.assertEqual(session.theater_name, cinema["name"])
                    self.assertEqual(session.displayed_at.strftime("%H:%M"), time)
                    self.assertEqual(visible_labels[session_id], {label.casefold() for label in labels})
                    self.assertTrue(visible_labels[session_id] <= {label.casefold() for label in session.labels})
        self.assertEqual([day.isoformat() for day in snapshot.published_dates], site["availableDates"])
        self.assertEqual(len(snapshot.sessions), 144)
        self.assertEqual(analyze(snapshot, self.profile).matching, ())
