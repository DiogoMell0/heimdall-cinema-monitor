"""Filtros por sessão e validação da programação."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest

from heimdall.cli import main
from heimdall.config import load_profile
from heimdall.models import Snapshot, ValidationError, parse_instant
from heimdall.rules import rejection_reasons
from heimdall.service import analyze
from heimdall.sources.ingresso import parse_sessions
from heimdall.sources.snapshot import load_snapshot

ROOT = Path(__file__).resolve().parent.parent
PROFILE_PATH = ROOT / "config/perfil.toml"
CAPTURE_PATH = ROOT / "tests/fixtures/programacao-inicial.json"
NOW = datetime.fromisoformat("2026-09-07T22:07:31-03:00")


def synthetic_payload() -> list:
    """Fixture mínima com uma sessão compatível."""
    return [{"date": "2026-09-24", "theaters": [{
        "id": "teste-cinema", "name": "Cinema fictício", "enabled": True, "blockMessage": "",
        "rooms": [{"name": "Sala fictícia", "sessions": [{
            "id": "teste-1", "type": ["Infinity Vision", "Legendado"],
            "realDate": {"localDate": "2026-09-24T20:00:00-03:00"},
            "date": {"localDate": "2026-09-24T20:00:00-03:00"},
            "enabled": True, "blockMessage": "",
            "siteURL": "https://checkout.ingresso.com/?sessionId=teste-1",
        }]}],
    }]}]


class RulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profile = load_profile(PROFILE_PATH)
        cls.capture = load_snapshot(CAPTURE_PATH, cls.profile)

    def session(self, payload=None):
        _, sessions = parse_sessions(
            synthetic_payload() if payload is None else payload,
            movie_id=self.profile.movie_id, city_id=self.profile.city_id,
        )
        return sessions[0]

    def test_fixture_matches_expected_normalized_sessions(self):
        expected = json.loads((ROOT / "tests/fixtures/sessoes-esperadas.json").read_text(encoding="utf-8-sig"))
        by_id = {session.id: session for session in self.capture.sessions}
        self.assertEqual(len(by_id), 137)
        self.assertEqual(set(by_id), {row["id"] for row in expected})
        for row in expected:
            with self.subTest(session=row["id"]):
                session = by_id[row["id"]]
                self.assertEqual(session.theater_name, row["theater"])
                self.assertEqual(session.starts_at, datetime.fromisoformat(row["startsAt"]))
                self.assertEqual(session.labels, frozenset(row["labels"]))
        result = analyze(self.capture, self.profile)
        self.assertEqual(result.matching, ())
        self.assertEqual(result.pending_dates, (date(2026, 10, 1),))
        self.assertEqual(len(self.capture.published_dates), 7)

    def test_synthetic_positive_is_accepted(self):
        self.assertEqual(rejection_reasons(self.session(), self.profile, now=NOW), ())

    def test_attributes_must_belong_to_same_session(self):
        first = replace(self.session(), labels=frozenset({"Infinity Vision", "Dublado"}))
        second = replace(self.session(), id="teste-2", labels=frozenset({"IMAX", "Legendado"}))
        result = analyze(Snapshot(NOW, (date(2026, 9, 24),), (first, second)), self.profile)
        self.assertEqual(result.matching, ())

    def test_exact_labels_and_case_normalization(self):
        for labels, accepted in [
            ({" infinity VISION ", "LEGENDADO"}, True),
            ({"IMAX", "Legendado"}, False),
            ({"Infinity Vision", "Não legendado"}, False),
            ({"Infinity Vision"}, False),
        ]:
            with self.subTest(labels=labels):
                session = replace(self.session(), labels=frozenset(labels))
                self.assertEqual(not rejection_reasons(session, self.profile, now=NOW), accepted)

    def test_period_boundaries_and_timezone(self):
        for instant, accepted in [
            ("2026-09-23T23:59:59-03:00", False),
            ("2026-09-24T00:00:00-03:00", True),
            ("2026-10-01T23:59:59-03:00", True),
            ("2026-10-02T00:00:00-03:00", False),
            ("2026-10-02T02:59:59+00:00", True),
            ("2026-10-02T03:00:00+00:00", False),
        ]:
            with self.subTest(instant=instant):
                session = replace(self.session(), starts_at=datetime.fromisoformat(instant))
                self.assertEqual(not rejection_reasons(session, self.profile, now=NOW), accepted)

    def test_actual_start_overrides_programming_day(self):
        payload = synthetic_payload()
        payload[0]["date"] = "2026-10-01"
        item = payload[0]["theaters"][0]["rooms"][0]["sessions"][0]
        item["date"]["localDate"] = "2026-10-01T23:59:00-03:00"
        item["realDate"]["localDate"] = "2026-10-02T00:10:00-03:00"
        self.assertIn("fora do período", rejection_reasons(self.session(payload), self.profile, now=NOW))

    def test_started_session_and_wrong_identity_are_rejected(self):
        base = self.session()
        self.assertIn("sessão já iniciada", rejection_reasons(base, self.profile, now=base.starts_at))
        for field in ("movie_id", "city_id"):
            with self.subTest(field=field):
                self.assertTrue(rejection_reasons(replace(base, **{field: "outro"}), self.profile, now=NOW))

    def test_disabled_or_blocked_sale_is_rejected(self):
        for target, field, value in [
            ("theater", "enabled", False), ("theater", "blockMessage", "Bloqueado"),
            ("session", "enabled", False), ("session", "blockMessage", "Bloqueado"),
            ("session", "siteURL", ""),
        ]:
            with self.subTest(target=target, field=field):
                payload = synthetic_payload()
                theater = payload[0]["theaters"][0]
                item = theater if target == "theater" else theater["rooms"][0]["sessions"][0]
                item[field] = value
                self.assertIn("compra não habilitada", rejection_reasons(self.session(payload), self.profile, now=NOW))

    def test_incomplete_response_is_an_error_not_zero_matches(self):
        for field in ("type", "realDate", "date", "enabled", "blockMessage", "siteURL", "id"):
            with self.subTest(field=field):
                payload = synthetic_payload()
                del payload[0]["theaters"][0]["rooms"][0]["sessions"][0][field]
                with self.assertRaises(ValidationError):
                    self.session(payload)
        for payload in (None, {}, [{"date": "2026-09-24"}], [{"date": "2026-09-24", "theaters": None}]):
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                parse_sessions(payload, movie_id="32174", city_id="14")

    def test_duplicate_ids_are_rejected(self):
        payload = synthetic_payload()
        sessions = payload[0]["theaters"][0]["rooms"][0]["sessions"]
        sessions.append(deepcopy(sessions[0]))
        with self.assertRaises(ValidationError):
            self.session(payload)

    def test_sale_status_must_be_boolean_and_link_must_match_session(self):
        for field, value in [("enabled", "false"), ("type", "Infinity Vision Legendado"),
                             ("siteURL", "https://checkout.ingresso.com/?sessionId=outra"),
                             ("siteURL", "https://[invalido")]:
            with self.subTest(field=field), self.assertRaises(ValidationError):
                payload = synthetic_payload()
                payload[0]["theaters"][0]["rooms"][0]["sessions"][0][field] = value
                self.session(payload)

    def test_new_date_is_discovered_without_changing_profile(self):
        payload = synthetic_payload()
        payload[0]["date"] = "2026-10-01"
        item = payload[0]["theaters"][0]["rooms"][0]["sessions"][0]
        item["realDate"]["localDate"] = item["date"]["localDate"] = "2026-10-01T20:00:00-03:00"
        dates, sessions = parse_sessions(payload, movie_id=self.profile.movie_id, city_id=self.profile.city_id)
        result = analyze(Snapshot(NOW, self.capture.published_dates + dates, self.capture.sessions + sessions), self.profile)
        self.assertEqual(result.pending_dates, ())
        self.assertEqual(len(result.matching), 1)

    def test_explicit_empty_programming_keeps_all_dates_pending(self):
        dates, sessions = parse_sessions([], movie_id="32174", city_id="14")
        result = analyze(Snapshot(NOW, dates, sessions), self.profile)
        self.assertEqual(len(result.pending_dates), 8)
        self.assertEqual(result.matching, ())

    def test_naive_time_is_rejected(self):
        with self.assertRaises(ValidationError):
            parse_instant("2026-09-24T20:00:00", "teste")
        with self.assertRaises(ValidationError):
            rejection_reasons(self.session(), self.profile, now=datetime(2026, 9, 7))

    def test_snapshot_provenance_status_and_body_are_required(self):
        envelope = {"source": "https://api-content.ingresso.com/v0/sessions/city/14/event/32174",
                    "status": 200, "checkedAt": NOW.isoformat(), "data": synthetic_payload()}
        for field, value in [("source", "https://api-content.ingresso.com/v0/sessions/city/1/event/32174"),
                             ("status", 404), ("status", 429), ("checkedAt", None), ("data", None)]:
            with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as folder:
                modified = {**envelope, field: value}
                path = Path(folder) / "capture.json"
                path.write_text(json.dumps(modified), encoding="utf-8")
                with self.assertRaises(ValidationError):
                    load_snapshot(path, self.profile)

    def test_invalid_profiles_are_rejected(self):
        original = PROFILE_PATH.read_text(encoding="utf-8")
        for content in ["[incompleto]", "[", original.replace("2026-10-02", "2026-09-23"),
                        original.replace("2026-10-02T00:00:00-03:00", "2026-10-02T00:00:00")]:
            with self.subTest(content=content[:30]), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "perfil.toml"
                path.write_text(content, encoding="utf-8")
                with self.assertRaises(ValidationError):
                    load_profile(path)

    def test_cli_labels_historical_result_and_reports_counts(self):
        output = StringIO()
        with redirect_stdout(output):
            code = main(["analisar", "--arquivo", str(CAPTURE_PATH)])
        self.assertEqual(code, 0)
        self.assertIn("OFFLINE", output.getvalue())
        self.assertIn("137 | Compatíveis: 0", output.getvalue())
        self.assertIn("01/10/2026", output.getvalue())

    def test_cli_reports_malformed_file_as_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "broken.json"
            path.write_text('{"data":', encoding="utf-8")
            output, error = StringIO(), StringIO()
            with redirect_stdout(output), redirect_stderr(error):
                code = main(["analisar", "--arquivo", str(path)])
        self.assertEqual(code, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("Falha na análise", error.getvalue())
