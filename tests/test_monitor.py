"""Agendamento, pausa e recuperação de ciclos."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from heimdall.cli import main
from heimdall.config import load_profile
from heimdall.locking import LockBusy, exclusive_lock
from heimdall.monitor import MonitorError, configure, diagnose, execute, load_config, set_paused
from heimdall.models import Snapshot
from heimdall.sources.http import CollectionError, _retry_at
from heimdall.sources.ingresso import parse_sessions
from heimdall.storage import History, HistoryError
from heimdall.tracking import TrackingResult
from tests.test_rules import PROFILE_PATH, synthetic_payload

NOW = datetime.fromisoformat("2026-09-15T12:00:00+00:00")


class MonitorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "control"
        self.database = self.directory / "history.sqlite3"
        self.profile = load_profile(PROFILE_PATH)
        _, self.sessions = parse_sessions(synthetic_payload(), movie_id=self.profile.movie_id, city_id=self.profile.city_id)
        self.now = NOW
        self.verify = Mock(side_effect=self.success)
        self.health = Mock()
        configure(self.directory, PROFILE_PATH, self.database, 30, now=NOW - timedelta(minutes=2))
        set_paused(self.directory, False, now=NOW)

    def success(self, profile, database):
        snapshot = Snapshot(self.now, tuple({row.programming_date for row in self.sessions}), self.sessions)
        with History(database) as history:
            changes = history.record_success(snapshot, profile)
        return TrackingResult(snapshot, changes), []

    def cycle(self, minutes=0):
        self.now = NOW + timedelta(minutes=minutes)
        return execute(self.directory, verifier=self.verify, clock=lambda: self.now, health_sender=self.health)

    def state(self):
        return json.loads((self.directory / "monitor-state.json").read_text(encoding="utf-8"))

    def next_cycle(self):
        self.now = datetime.fromisoformat(self.state()["next_due"])
        return execute(self.directory, verifier=self.verify, clock=lambda: self.now, health_sender=self.health)

    def test_configuration_starts_paused_without_database_or_network(self):
        other = self.directory / "other"
        result = configure(other, PROFILE_PATH, other / "db.sqlite3", 30, now=NOW)
        self.assertTrue(result["paused"])
        self.assertTrue(Path(result["database"]).is_absolute())
        self.assertFalse((other / "db.sqlite3").exists())
        self.verify.assert_not_called()

    def test_cycle_interval_is_preserved_after_reopening(self):
        self.assertEqual(self.cycle(), 0)
        self.assertEqual(self.cycle(minutes=29), 0)
        self.assertEqual(self.verify.call_count, 1)
        self.assertEqual(self.cycle(minutes=30), 0)
        self.assertEqual(self.verify.call_count, 2)
        self.assertEqual(self.state()["cycles"], 2)
        self.health.assert_not_called()

    def test_scheduler_jitter_does_not_skip_every_other_cycle(self):
        self.now = NOW + timedelta(seconds=2)
        execute(self.directory, verifier=self.verify, clock=lambda: self.now, health_sender=self.health)
        self.now = NOW + timedelta(minutes=30, seconds=1)
        execute(self.directory, verifier=self.verify, clock=lambda: self.now, health_sender=self.health)
        self.assertEqual(self.verify.call_count, 2)

    def test_paused_monitor_does_not_access_source(self):
        set_paused(self.directory, True, now=NOW)
        self.assertEqual(self.cycle(), 0)
        self.verify.assert_not_called()
        self.assertFalse(self.database.exists())

    def test_end_of_period_stops_before_network_and_cannot_resume(self):
        self.now = self.profile.ends_before
        self.assertEqual(execute(self.directory, verifier=self.verify, clock=lambda: self.now), 0)
        self.verify.assert_not_called()
        self.assertEqual(self.state()["status"], "completed")
        with self.assertRaises(MonitorError):
            set_paused(self.directory, False, now=self.now)

    def test_failure_backoff_persists_and_increases(self):
        self.verify.side_effect = CollectionError("rede", "fictício")
        self.assertEqual(self.cycle(), 1)
        self.assertEqual(datetime.fromisoformat(self.state()["next_due"]), NOW + timedelta(minutes=30))
        self.cycle(minutes=29)
        self.assertEqual(self.verify.call_count, 1)
        self.next_cycle()
        self.assertEqual(datetime.fromisoformat(self.state()["next_due"]), NOW + timedelta(minutes=90))
        self.assertEqual(self.state()["consecutive_failures"], 2)

    def test_source_retry_after_is_not_shortened(self):
        allowed = NOW + timedelta(days=2)
        self.verify.side_effect = CollectionError("limite_de_acesso", "fictício", status=429, retry_at=allowed)
        self.cycle()
        self.assertEqual(datetime.fromisoformat(self.state()["next_due"]), allowed)
        self.cycle(minutes=60)
        self.assertEqual(self.verify.call_count, 1)

    def test_pause_and_resume_preserve_server_wait(self):
        self.verify.side_effect = CollectionError("limite_de_acesso", "fictício", retry_at=NOW + timedelta(hours=4))
        self.cycle()
        due = self.state()["next_due"]
        set_paused(self.directory, True, now=NOW)
        set_paused(self.directory, False, now=NOW)
        self.assertEqual(self.state()["next_due"], due)
        self.cycle(minutes=30)
        self.assertEqual(self.verify.call_count, 1)

    def test_access_refusal_blocks_recurrence_and_warns_once(self):
        self.verify.side_effect = CollectionError("acesso_recusado", "fictício", status=403)
        self.cycle()
        self.cycle(minutes=120)
        self.assertEqual(self.verify.call_count, 1)
        self.assertTrue(self.state()["blocked"])
        self.health.assert_called_once()

    def test_three_failures_warn_once_and_recovery_warns_once(self):
        self.verify.side_effect = CollectionError("timeout", "fictício")
        self.cycle()
        self.next_cycle()
        self.health.assert_not_called()
        self.next_cycle()
        self.next_cycle()
        self.assertEqual(self.health.call_count, 1)
        self.verify.side_effect = self.success
        self.next_cycle()
        self.next_cycle()
        self.assertEqual(self.health.call_count, 2)
        self.assertIn("RECUPERADO", self.health.call_args.args[0])
        self.assertEqual(self.state()["consecutive_failures"], 0)

    def test_health_notice_is_reserved_before_network_and_not_retried(self):
        def failure(message):
            self.assertTrue(self.state()["failure_notice_attempted"])
            self.assertEqual(self.state()["last_health_notice"], "sending")
            raise RuntimeError("sensitive-fake-secret")
        self.health.side_effect = failure
        self.verify.side_effect = CollectionError("timeout", "fictício")
        self.cycle()
        self.next_cycle()
        self.next_cycle()
        self.next_cycle()
        self.health.assert_called_once()
        self.assertEqual(self.state()["last_health_notice"], "unconfirmed")
        self.assertNotIn("sensitive-fake-secret", (self.directory / "monitor.log").read_text())

    def test_cycle_reservation_is_written_before_verifier(self):
        def check(profile, database):
            self.assertEqual(self.state()["status"], "running")
            self.assertIn("next_due", self.state())
            return self.success(profile, database)
        self.verify.side_effect = check
        self.cycle()

    def test_failed_reservation_does_not_query(self):
        with patch("heimdall.monitor._write", side_effect=OSError("test")):
            with self.assertRaises(OSError):
                self.cycle()
        self.verify.assert_not_called()

    def test_interrupted_cycle_recovers_without_immediate_retry(self):
        self.verify.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.cycle()
        self.verify.side_effect = self.success
        self.cycle(minutes=1)
        self.assertEqual(self.verify.call_count, 1)
        self.assertEqual(self.state()["status"], "interrupted")
        self.cycle(minutes=30)
        self.assertEqual(self.verify.call_count, 2)
        self.assertIn("previous_cycle_interrupted", (self.directory / "monitor.log").read_text())

    def test_large_gap_triggers_one_current_query_not_catchup_loop(self):
        self.cycle()
        self.cycle(minutes=300)
        self.assertEqual(self.verify.call_count, 2)
        self.assertIn("coverage_gap", (self.directory / "monitor.log").read_text())

    def test_repeated_process_interruptions_are_counted_and_warned(self):
        self.verify.side_effect = KeyboardInterrupt()
        for minute in (0, 30, 60, 90):
            with self.assertRaises(KeyboardInterrupt):
                self.cycle(minutes=minute)
        self.assertEqual(self.state()["consecutive_failures"], 3)
        self.health.assert_called_once()
        self.assertIn("interrupted", self.health.call_args.args[0])

    def test_second_monitor_process_is_blocked(self):
        with exclusive_lock(self.directory / "monitor.lock"):
            with self.assertRaises(LockBusy):
                self.cycle()
        self.verify.assert_not_called()
        self.assertEqual(self.cycle(), 0)

    def test_unexpected_error_is_sanitized_and_blocks_recurrence(self):
        self.verify.side_effect = RuntimeError("sensitive-fake-secret")
        self.cycle()
        self.assertTrue(self.state()["blocked"])
        self.assertEqual(self.state()["last_error"], "unexpected_error")
        self.assertNotIn("sensitive-fake-secret", (self.directory / "monitor-state.json").read_text())
        self.assertNotIn("sensitive-fake-secret", (self.directory / "monitor.log").read_text())

    def test_storage_failure_is_visible_without_exception_text(self):
        self.verify.side_effect = HistoryError("private-path-or-detail")
        self.cycle()
        self.assertEqual(self.state()["last_error"], "local_storage_or_configuration")
        self.health.assert_called_once()
        self.assertNotIn("private-path-or-detail", self.health.call_args.args[0])

    def test_delivery_problem_keeps_collection_time_and_marks_failure(self):
        def check(profile, database):
            result, _ = self.success(profile, database)
            return result, [{"status": "uncertain"}]
        self.verify.side_effect = check
        self.assertEqual(self.cycle(), 1)
        self.assertEqual(self.state()["last_error"], "delivery_attention")
        self.assertEqual(datetime.fromisoformat(self.state()["last_collection"]), NOW)

    def test_status_is_read_only_and_missing_config_creates_nothing(self):
        missing = self.directory / "missing"
        with redirect_stderr(StringIO()), redirect_stdout(StringIO()):
            self.assertEqual(main(["monitor", "status", "--diretorio", str(missing)]), 1)
        self.assertFalse(missing.exists())
        original = (self.directory / "monitor-state.json").read_bytes()
        with redirect_stdout(StringIO()):
            self.assertEqual(main(["monitor", "status", "--diretorio", str(self.directory)]), 0)
        self.assertEqual((self.directory / "monitor-state.json").read_bytes(), original)

    def test_corrupt_state_is_not_replaced_or_used_for_queries(self):
        path = self.directory / "monitor-state.json"
        path.write_text('{"broken":', encoding="utf-8")
        with self.assertRaises(MonitorError):
            self.cycle()
        self.verify.assert_not_called()
        self.assertEqual(path.read_text(), '{"broken":')

    def test_changed_profile_key_is_rejected_before_network(self):
        path = self.directory / "monitor.json"
        config = load_config(self.directory)
        config["profile_key"] = "different"
        path.write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaises(MonitorError):
            self.cycle()
        self.verify.assert_not_called()

    def test_reconfigure_requires_pause_and_preserves_existing_state(self):
        with self.assertRaises(MonitorError):
            configure(self.directory, PROFILE_PATH, self.database, 60, now=NOW)
        set_paused(self.directory, True, now=NOW)
        original = (self.directory / "monitor-state.json").read_bytes()
        configure(self.directory, PROFILE_PATH, self.database, 60, now=NOW)
        self.assertEqual(load_config(self.directory)["interval_minutes"], 60)
        self.assertEqual((self.directory / "monitor-state.json").read_bytes(), original)

    def test_diagnostic_reads_credentials_and_database_without_network(self):
        self.success(self.profile, self.database)
        with patch("heimdall.monitor.load_credentials") as credentials, patch("heimdall.monitor.verify_and_notify") as verify:
            result = diagnose(self.directory, now=NOW)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["network_calls"], 0)
        credentials.assert_called_once()
        verify.assert_not_called()

    def test_failed_diagnostic_does_not_create_database_or_leak_error(self):
        with patch("heimdall.monitor.load_credentials", side_effect=ValueError("sensitive-fake-secret")):
            result = diagnose(self.directory, now=NOW)
        self.assertEqual(result["status"], "failure")
        self.assertFalse(self.database.exists())
        self.assertNotIn("sensitive-fake-secret", json.dumps(result))

    def test_retry_header_dates_seconds_and_extreme_values(self):
        self.assertEqual(_retry_at("120", NOW), NOW + timedelta(seconds=120))
        self.assertEqual(_retry_at("Tue, 15 Sep 2026 16:00:00 GMT", NOW), NOW + timedelta(hours=4))
        self.assertEqual(_retry_at("9" * 200, NOW), datetime.max.replace(tzinfo=timezone.utc))
        for value in [None, "", "invalid", "-1"]:
            self.assertIsNone(_retry_at(value, NOW))


if __name__ == "__main__":
    unittest.main()
