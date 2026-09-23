"""Resumos de consultas e falhas de envio."""

from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from heimdall.cli import main
from heimdall.config import load_profile
from heimdall.delivery import DeliveryError
from heimdall.models import Snapshot
from heimdall.monitor import MonitorError, configure, execute, load_config, set_feedback, set_paused
from heimdall.sources.http import CollectionError
from heimdall.sources.ingresso import parse_sessions
from heimdall.storage import History, HistoryError
from heimdall.tracking import TrackingResult
from tests.test_rules import PROFILE_PATH, synthetic_payload

NOW = datetime.fromisoformat("2026-09-15T12:00:00+00:00")


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "monitor"
        self.database = self.directory / "history.sqlite3"
        self.profile = load_profile(PROFILE_PATH)
        _, self.sessions = parse_sessions(synthetic_payload(), movie_id=self.profile.movie_id, city_id=self.profile.city_id)
        self.now = NOW
        self.deliveries = []
        self.sender = Mock()
        self.health = Mock()
        self.verifier = Mock(side_effect=self.success)
        configure(self.directory, PROFILE_PATH, self.database, 30, now=NOW - timedelta(minutes=2))
        set_paused(self.directory, False, now=NOW)
        set_feedback(self.directory, True, now=NOW)

    def success(self, profile, database):
        snapshot = Snapshot(self.now, tuple({s.programming_date for s in self.sessions}), self.sessions)
        with History(database) as history:
            changes = history.record_success(snapshot, profile)
        return TrackingResult(snapshot, changes), self.deliveries

    def cycle(self, minutes=0):
        self.now = NOW + timedelta(minutes=minutes)
        return execute(self.directory, clock=lambda: self.now, verifier=self.verifier,
                       health_sender=self.health, feedback_sender=self.sender)

    def state(self):
        return json.loads((self.directory / "monitor-state.json").read_text(encoding="utf-8"))

    def message(self):
        return self.sender.call_args.args[0]

    def test_legacy_config_remains_silent_without_rewriting_it(self):
        path = self.directory / "monitor.json"
        config = load_config(self.directory)
        config.pop("cycle_feedback")
        path.write_text(json.dumps(config), encoding="utf-8")
        original = path.read_bytes()
        self.cycle()
        self.sender.assert_not_called()
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(load_config(self.directory)["cycle_feedback"])

    def test_cli_toggle_preserves_pause_profile_interval_wait_and_history(self):
        self.verifier.side_effect = CollectionError("limite_de_acesso", "fake", retry_at=NOW + timedelta(days=2))
        self.cycle()
        set_paused(self.directory, True, now=NOW)
        original = (self.directory / "monitor-state.json").read_bytes()
        config_before = load_config(self.directory)
        for action in ("desativar", "ativar"):
            with redirect_stdout(StringIO()):
                self.assertEqual(main(["monitor", "feedback", action, "--diretorio", str(self.directory)]), 0)
            self.assertEqual((self.directory / "monitor-state.json").read_bytes(), original)
            current = load_config(self.directory)
            self.assertEqual({k: v for k, v in current.items() if k != "cycle_feedback"},
                             {k: v for k, v in config_before.items() if k != "cycle_feedback"})
        configure(self.directory, PROFILE_PATH, self.database, 60, now=NOW)
        self.assertTrue(load_config(self.directory)["cycle_feedback"])

    def test_invalid_feedback_configuration_rejected_before_query(self):
        path = self.directory / "monitor.json"
        config = load_config(self.directory)
        config["cycle_feedback"] = "true"
        path.write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaises(MonitorError):
            self.cycle()
        self.verifier.assert_not_called()
        self.sender.assert_not_called()

    def test_success_has_counts_coverage_local_time_and_next_consultation(self):
        self.assertEqual(self.cycle(), 0)
        for expected in ("CONSULTA #1", "15/09/2026 09:00:00 -0300", "Sessões retornadas pela API: 1",
                         "Compatíveis com seus filtros: 1", "IDs novos: 1", "Novidades compatíveis: 1",
                         "Datas publicadas no período: 1/8", "25/09", "01/10", "09:30:00 -0300"):
            self.assertIn(expected, self.message())
        self.assertEqual(self.state()["last_cycle_feedback"]["status"], "sent")
        self.assertLess(len(self.message().encode("utf-16-le")) // 2, 3800)
        self.assertEqual(self.verifier.call_count, 1)
        self.assertEqual(self.health.call_count, 0)

    def test_unchanged_next_cycle_still_reports_without_duplicate_events(self):
        self.cycle()
        self.cycle(30)
        self.assertEqual(self.sender.call_count, 2)
        self.assertIn("CONSULTA #2", self.message())
        self.assertIn("IDs novos: 0", self.message())
        self.assertIn("Novidades compatíveis: 0", self.message())
        self.assertIn("Compatíveis com seus filtros: 1", self.message())

    def test_valid_empty_response_is_distinct_from_failure(self):
        self.sessions = ()
        self.cycle()
        self.assertIn("Concluída", self.message())
        self.assertIn("Sessões retornadas pela API: 0", self.message())
        self.assertIn("Datas publicadas no período: 0/8", self.message())
        self.assertNotIn("Sem resultado completo", self.message())

    def test_failed_query_reports_no_current_counts_after_success(self):
        self.cycle()
        self.verifier.side_effect = CollectionError("rede", "sensitive-fake-secret")
        self.assertEqual(self.cycle(30), 1)
        self.assertIn("Sem resultado completo", self.message())
        self.assertIn("Última consulta válida anterior: 15/09/2026 09:00:00", self.message())
        self.assertIn("Falha de conexão", self.message())
        self.assertIn("Falhas consecutivas: 1", self.message())
        self.assertNotIn("Sessões retornadas pela API:", self.message())
        self.assertNotIn("sensitive-fake-secret", self.message())
        self.assertIsNone(self.state()["cycle_summary"])

    def test_access_refusal_reports_interruption_not_next_query(self):
        self.verifier.side_effect = CollectionError("acesso_recusado", "fake", status=403)
        self.cycle()
        self.assertIn("A fonte recusou o acesso", self.message())
        self.assertIn("Consultas interrompidas", self.message())
        self.assertNotIn("Próxima consulta:", self.message())
        self.cycle(120)
        self.sender.assert_called_once()

    def test_report_counts_sent_events_separately_from_messages(self):
        self.deliveries = [{"status": "sent", "events": 3}, {"status": "sent", "events": 2}, {"status": "uncertain"}]
        counts = {"counts": [{"status": "pending", "total": 4}, {"status": "uncertain", "total": 1}]}
        with patch("heimdall.monitor.read_deliveries", return_value=counts):
            self.assertEqual(self.cycle(), 1)
        self.assertIn("2 mensagem(ns), 5 sessão(ões)", self.message())
        self.assertIn("Avisos pendentes: 4 | Incertos: 1", self.message())
        self.assertIn("Consulta concluída; há avisos", self.message())

    def test_queue_read_failure_keeps_fresh_summary_but_not_old_delivery_counts(self):
        self.cycle()
        with patch("heimdall.monitor.read_deliveries", side_effect=HistoryError("sensitive-fake-secret")):
            self.assertEqual(self.cycle(30), 1)
        self.assertIn("Sessões retornadas pela API: 1", self.message())
        self.assertIn("Não foi possível conferir a fila", self.message())
        self.assertNotIn("Avisos pendentes:", self.message())
        self.assertNotIn("sensitive-fake-secret", self.message())

    def test_skipped_ticks_and_end_do_not_send_feedback(self):
        self.cycle()
        self.cycle(29)
        set_paused(self.directory, True, now=NOW)
        self.cycle(30)
        self.now = self.profile.ends_before
        execute(self.directory, clock=lambda: self.now, verifier=self.verifier, feedback_sender=self.sender)
        self.sender.assert_called_once()
        self.verifier.assert_called_once()

    def test_disabled_feedback_keeps_existing_health_alerts(self):
        set_feedback(self.directory, False, now=NOW)
        self.verifier.side_effect = CollectionError("acesso_recusado", "fake", status=403)
        self.cycle()
        self.sender.assert_not_called()
        self.health.assert_called_once()

    def test_feedback_failure_does_not_fail_or_repeat_successful_collection(self):
        self.sender.side_effect = RuntimeError("sensitive-fake-secret")
        self.assertEqual(self.cycle(), 0)
        self.cycle(1)
        state = self.state()
        self.assertEqual(state["status"], "ok")
        self.assertEqual(state["consecutive_failures"], 0)
        self.assertEqual(state["last_cycle_feedback"]["status"], "unconfirmed")
        self.sender.assert_called_once()
        self.verifier.assert_called_once()
        for path in ("monitor-state.json", "monitor.log"):
            self.assertNotIn("sensitive-fake-secret", (self.directory / path).read_text())
        output = StringIO()
        with redirect_stdout(output):
            main(["monitor", "status", "--diretorio", str(self.directory)])
        self.assertIn("unconfirmed", output.getvalue())

    def test_report_reservation_precedes_send_and_crash_is_not_retried(self):
        def interrupt(message):
            self.assertEqual(self.state()["status"], "ok")
            self.assertEqual(self.state()["last_cycle_feedback"]["status"], "sending")
            raise KeyboardInterrupt()
        self.sender.side_effect = interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.cycle()
        self.sender.side_effect = None
        self.cycle(1)
        self.assertEqual(self.state()["last_cycle_feedback"]["status"], "unconfirmed")
        self.sender.assert_called_once()
        self.cycle(30)
        self.assertEqual(self.sender.call_count, 2)
        self.assertIn("CONSULTA #2", self.message())
        self.assertEqual(self.state()["consecutive_failures"], 0)

    def test_failed_report_reservation_prevents_send(self):
        from heimdall.monitor import _write
        def write(path, value):
            if value.get("last_cycle_feedback", {}).get("status") == "sending":
                raise OSError("fake-disk-failure")
            _write(path, value)
        with patch("heimdall.monitor._write", side_effect=write):
            with self.assertRaises(OSError):
                self.cycle()
        self.sender.assert_not_called()
        self.assertEqual(self.state()["status"], "ok")

    def test_telegram_wait_survives_reopening_without_delaying_source(self):
        self.sender.side_effect = DeliveryError("fake-rejected", retry_after=7200)
        self.cycle()
        self.assertEqual(self.state()["last_cycle_feedback"]["status"], "failed")
        set_feedback(self.directory, False, now=NOW)
        set_feedback(self.directory, True, now=NOW)
        self.sender.side_effect = None
        self.cycle(30)
        self.assertEqual(self.state()["last_cycle_feedback"]["status"], "deferred")
        self.assertEqual(self.verifier.call_count, 2)
        self.sender.assert_called_once()
        self.cycle(120)
        self.assertEqual(self.sender.call_count, 2)
        self.assertIn("CONSULTA #3", self.message())
        self.assertNotIn("feedback_retry_at", self.state())

    def test_extreme_telegram_wait_is_safe(self):
        self.sender.side_effect = DeliveryError("fake-rejected", retry_after=10 ** 100)
        self.assertEqual(self.cycle(), 0)
        self.assertEqual(datetime.fromisoformat(self.state()["feedback_retry_at"]), datetime.max.replace(tzinfo=timezone.utc))
        self.cycle(30)
        self.sender.assert_called_once()


if __name__ == "__main__":
    unittest.main()
