"""Fila de entregas, migração e recuperação de falhas."""

from contextlib import closing, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta
from io import StringIO
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError

from heimdall.cli import main
from heimdall.config import load_profile
from heimdall.delivery import DeliveryError, DeliveryQueue, notification_lock, read_deliveries
from heimdall.models import Snapshot
from heimdall.notifications import verify_and_notify
from heimdall.notifiers import make_notifier
from heimdall.sources.http import Collection, CollectionError
from heimdall.sources.ingresso import parse_sessions
from heimdall.storage import History, HistoryError, SCHEMA, read_history
from heimdall.telegram import TelegramClient, TelegramError
from tests.test_rules import PROFILE_PATH, synthetic_payload

NOW = datetime.fromisoformat("2026-09-14T17:00:00-03:00")


class FakeNotifier:
    target_key = "test-recipient"

    def __init__(self, failure=None):
        self.calls = []
        self.failure = failure

    def send(self, batch):
        self.calls.append(batch)
        if self.failure:
            raise self.failure
        return "receipt-for-" + batch["id"]


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.database = Path(folder.name) / "history.sqlite3"
        self.profile = load_profile(PROFILE_PATH)
        _, sessions = parse_sessions(synthetic_payload(), movie_id=self.profile.movie_id, city_id=self.profile.city_id)
        self.matching = sessions[0]
        self.telegram = FakeNotifier()
        self.senders = {"telegram": self.telegram}

    def snapshot(self, sessions=None, step=0):
        rows = tuple(sessions if sessions is not None else [self.matching])
        return Snapshot(NOW + timedelta(minutes=step), tuple(sorted({row.programming_date for row in rows})), rows)

    def run_cycle(self, sessions=None, step=0, channels=("telegram",)):
        snapshot = self.snapshot(sessions, step)
        with patch("heimdall.tracking.collect", return_value=Collection(snapshot, {})):
            return verify_and_notify(self.profile, self.database, channels=channels,
                                     factory=self.senders.__getitem__, clock=lambda: snapshot.captured_at)

    def counts(self):
        return {(row["channel"], row["status"]): row["total"] for row in read_deliveries(self.database, self.profile)["counts"]}

    def test_groups_new_events_and_repeat_after_reopen_sends_nothing(self):
        sessions = [self.matching, replace(self.matching, id="other-session")]
        self.run_cycle(sessions)
        result, sent = self.run_cycle(sessions, step=1)
        self.assertEqual(sent, [])
        self.assertEqual(len(self.telegram.calls), 1)
        self.assertEqual(len(self.telegram.calls[0]["events"]), 2)
        self.assertIn(self.matching.purchase_url, self.telegram.calls[0]["message"])
        self.assertEqual(self.counts(), {("telegram", "sent"): 2})
        self.assertEqual(result.changes.new_events, ())

    def test_failure_keeps_pending_and_success_is_not_repeated(self):
        self.telegram.failure = DeliveryError("telegram_rejected")
        self.run_cycle()
        self.assertEqual(self.counts(), {("telegram", "pending"): 1})
        self.telegram.failure = None
        self.run_cycle(step=1)
        self.run_cycle(step=2)
        self.assertEqual(len(self.telegram.calls), 2)
        self.assertEqual(self.counts(), {("telegram", "sent"): 1})

    def test_retry_after_is_preserved_between_processes(self):
        self.telegram.failure = DeliveryError("rate_limit", retry_after=180)
        self.run_cycle()
        self.telegram.failure = None
        self.run_cycle(step=1)
        self.assertEqual(len(self.telegram.calls), 1)
        self.run_cycle(step=3)
        self.assertEqual(len(self.telegram.calls), 2)

    def test_retry_after_longer_than_one_day_is_not_shortened(self):
        self.telegram.failure = DeliveryError("rate_limit", retry_after=172800)
        self.run_cycle()
        self.telegram.failure = None
        self.run_cycle(step=1440)
        self.assertEqual(len(self.telegram.calls), 1)
        self.run_cycle(step=2880)
        self.assertEqual(len(self.telegram.calls), 2)

    def test_extreme_retry_after_stays_pending_without_overflow(self):
        self.telegram.failure = DeliveryError("rate_limit", retry_after=10**100)
        self.run_cycle()
        self.telegram.failure = None
        self.run_cycle(step=1440)
        self.assertEqual(len(self.telegram.calls), 1)
        self.assertEqual(self.counts(), {("telegram", "pending"): 1})

    def test_uncertain_send_is_not_retried_until_explicit_resolution(self):
        self.telegram.failure = DeliveryError("telegram_uncertain", uncertain=True)
        self.run_cycle()
        self.telegram.failure = None
        self.run_cycle(step=1)
        self.assertEqual(len(self.telegram.calls), 1)
        self.assertEqual(self.counts()[("telegram", "uncertain")], 1)
        batch_id = self.telegram.calls[0]["id"]
        with notification_lock(self.database), History(self.database) as history:
            DeliveryQueue(history, self.profile).resolve(batch_id, "tentar-novamente", NOW)
        self.run_cycle(step=2)
        self.assertEqual(len(self.telegram.calls), 2)

    def test_confirming_uncertain_delivery_does_not_resend(self):
        self.telegram.failure = DeliveryError("unknown", uncertain=True)
        self.run_cycle()
        with notification_lock(self.database), History(self.database) as history:
            DeliveryQueue(history, self.profile).resolve(self.telegram.calls[0]["id"], "confirmar", NOW)
        self.telegram.failure = None
        self.run_cycle(step=1)
        self.assertEqual(len(self.telegram.calls), 1)
        self.assertEqual(self.counts()[("telegram", "sent")], 1)

    def test_interrupted_send_is_recovered_even_when_next_collection_fails(self):
        with History(self.database) as history:
            run = history.record_success(self.snapshot(), self.profile)
            queue = DeliveryQueue(history, self.profile)
            queue.enqueue(["telegram"], NOW)
            queue.claim("telegram", "recipient", run.run_id, NOW)
        with patch("heimdall.tracking.collect", side_effect=CollectionError("timeout", "test")):
            with self.assertRaises(CollectionError):
                verify_and_notify(self.profile, self.database, factory=self.senders.__getitem__, clock=lambda: NOW)
        self.assertEqual(self.counts(), {("telegram", "uncertain"): 1})
        self.assertEqual(self.telegram.calls, [])

    def test_failed_collection_never_dispatches_old_pending_events(self):
        self.telegram.failure = DeliveryError("rejected")
        self.run_cycle(channels=["telegram"])
        with patch("heimdall.tracking.collect", side_effect=CollectionError("timeout", "test")):
            with self.assertRaises(CollectionError):
                verify_and_notify(self.profile, self.database, factory=self.senders.__getitem__)
        self.assertEqual(len(self.telegram.calls), 1)

    def test_absent_session_waits_and_reappearance_uses_same_event(self):
        self.telegram.failure = DeliveryError("rejected")
        self.run_cycle(channels=["telegram"])
        self.telegram.failure = None
        self.run_cycle([], step=1, channels=["telegram"])
        self.assertEqual(self.counts(), {("telegram", "pending"): 1})
        self.assertEqual(len(self.telegram.calls), 1)
        self.run_cycle(step=2, channels=["telegram"])
        self.assertEqual(len(self.telegram.calls), 2)
        self.assertEqual(read_history(self.database, self.profile)["event_count"], 1)

    def test_ineligible_then_matching_obsoletes_old_event_and_sends_new_transition(self):
        self.telegram.failure = DeliveryError("rejected")
        self.run_cycle(channels=["telegram"])
        self.telegram.failure = None
        dubbed = replace(self.matching, labels=frozenset({"Infinity Vision", "Dublado"}))
        self.run_cycle([dubbed], step=1, channels=["telegram"])
        self.assertEqual(self.counts(), {("telegram", "obsolete"): 1})
        self.run_cycle(step=2, channels=["telegram"])
        self.assertEqual(len(self.telegram.calls), 2)
        self.assertNotEqual(self.telegram.calls[0]["events"], self.telegram.calls[1]["events"])

    def test_pending_notice_uses_updated_time_and_not_event_snapshot(self):
        self.telegram.failure = DeliveryError("rejected")
        self.run_cycle(channels=["telegram"])
        self.telegram.failure = None
        changed = replace(self.matching, starts_at=self.matching.starts_at + timedelta(hours=1))
        self.run_cycle([changed], step=1, channels=["telegram"])
        self.assertIn(changed.starts_at.strftime("%H:%M"), self.telegram.calls[1]["message"])
        self.assertEqual(read_history(self.database, self.profile)["event_count"], 1)

    def test_expired_session_is_obsolete_even_if_last_capture_matched(self):
        with History(self.database) as history:
            captured = self.matching.starts_at - timedelta(seconds=2)
            snapshot = replace(self.snapshot(), captured_at=captured)
            run = history.record_success(snapshot, self.profile)
            queue = DeliveryQueue(history, self.profile)
            queue.enqueue(["telegram"], captured)
            self.assertIsNone(queue.claim("telegram", "recipient", run.run_id, captured + timedelta(seconds=3)))
        self.assertEqual(self.counts(), {("telegram", "obsolete"): 1})

    def test_stale_or_replaced_run_cannot_send(self):
        with History(self.database) as history:
            first = history.record_success(self.snapshot(), self.profile)
            queue = DeliveryQueue(history, self.profile)
            queue.enqueue(["telegram"], NOW)
            with self.assertRaises(HistoryError):
                queue.claim("telegram", "recipient", first.run_id, NOW + timedelta(minutes=6))
            history.record_success(self.snapshot(step=1), self.profile)
            with self.assertRaises(HistoryError):
                queue.claim("telegram", "recipient", first.run_id, NOW + timedelta(minutes=1))

    def test_reservation_is_committed_before_network_send(self):
        original = self.telegram.send
        def inspect_then_send(batch):
            with closing(sqlite3.connect(self.database)) as db:
                self.assertEqual(db.execute("SELECT status FROM delivery_batches WHERE id=?", (batch["id"],)).fetchone()[0], "sending")
            return original(batch)
        self.telegram.send = inspect_then_send
        self.run_cycle(channels=["telegram"])

    def test_failed_receipt_commit_recovers_as_uncertain_instead_of_resending(self):
        with History(self.database) as history:
            history.connection.execute("""CREATE TRIGGER fail_receipt BEFORE UPDATE OF status ON delivery_batches
                WHEN NEW.status='sent' BEGIN SELECT RAISE(ABORT, 'simulated crash'); END""")
        with self.assertRaises(HistoryError):
            self.run_cycle(channels=["telegram"])
        with History(self.database) as history:
            history.connection.execute("DROP TRIGGER fail_receipt")
        self.run_cycle(step=1, channels=["telegram"])
        self.assertEqual(len(self.telegram.calls), 1)
        self.assertEqual(self.counts(), {("telegram", "uncertain"): 1})

    def test_process_lock_blocks_second_sender_and_is_released_after_failure(self):
        with notification_lock(self.database):
            with self.assertRaises(HistoryError):
                self.run_cycle()
        self.run_cycle()
        self.assertEqual(len(self.telegram.calls), 1)

    def test_replay_is_refused_before_network_or_any_notification(self):
        with History(self.database, dataset="replay"):
            pass
        with patch("heimdall.tracking.collect") as collect:
            with self.assertRaises(HistoryError):
                verify_and_notify(self.profile, self.database, factory=self.senders.__getitem__)
        collect.assert_not_called()
        self.assertEqual(self.telegram.calls, [])

    def test_configuration_failure_preserves_pending_events(self):
        def factory(channel):
            raise DeliveryError("telegram_configuration")
        with patch("heimdall.tracking.collect", return_value=Collection(self.snapshot(), {})):
            _, results = verify_and_notify(self.profile, self.database, factory=factory, clock=lambda: NOW)
        self.assertEqual(results[0]["status"], "configuration_error")
        self.assertEqual(self.counts(), {("telegram", "pending"): 1})

    def test_unexpected_sender_exception_does_not_save_its_sensitive_text(self):
        self.telegram.failure = RuntimeError("secret-in-exception")
        _, results = self.run_cycle(channels=["telegram"])
        self.assertEqual(results[0]["status"], "uncertain")
        self.assertNotIn("secret-in-exception", json.dumps(read_deliveries(self.database, self.profile)))

    def test_large_collection_splits_messages_within_limits(self):
        rows = [replace(self.matching, id=str(index), theater_name="Cinema " + "x" * 170) for index in range(26)]
        self.run_cycle(rows)
        self.assertGreater(len(self.telegram.calls), 1)
        self.assertEqual(sum(len(batch["events"]) for batch in self.telegram.calls), 26)
        self.assertTrue(all(len(batch["message"].encode("utf-16-le")) // 2 <= 3800 for batch in self.telegram.calls))

    def test_profile_isolation(self):
        self.run_cycle(channels=["telegram"])
        self.run_cycle(step=1)
        self.assertEqual(len(self.telegram.calls), 1)
        unrelated = replace(self.profile, movie_id="999")
        self.assertEqual(read_deliveries(self.database, unrelated), {"counts": [], "batches": []})

    def test_schema_v1_migration_preserves_history_and_makes_sqlite_backup(self):
        with closing(sqlite3.connect(self.database)) as db:
            for statement in SCHEMA:
                db.execute(statement)
            db.executemany("INSERT INTO metadata VALUES (?, ?)", [("application", "heimdall"), ("dataset", "online")])
            db.execute("PRAGMA user_version=1")
            db.commit()
        with History(self.database, read_only=True) as history:
            self.assertEqual(history.connection.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertFalse(Path(str(self.database) + ".before-v2.bak").exists())
        with History(self.database) as history:
            self.assertEqual(history.connection.execute("PRAGMA user_version").fetchone()[0], 2)
        backup = Path(str(self.database) + ".before-v2.bak")
        with closing(sqlite3.connect(backup)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT value FROM metadata WHERE key='application'").fetchone()[0], "heimdall")
        original_backup = backup.read_bytes()
        self.run_cycle()
        self.assertEqual(backup.read_bytes(), original_backup)

    def test_read_missing_delivery_history_creates_nothing(self):
        self.assertEqual(read_deliveries(self.database, self.profile), {"counts": [], "batches": []})
        self.assertFalse(self.database.exists())

    def make_populated_v1_database(self):
        with History(self.database) as history:
            history.record_success(self.snapshot(), self.profile)
            history.connection.execute("DROP TABLE deliveries")
            history.connection.execute("DROP TABLE delivery_batches")
            history.connection.execute("PRAGMA user_version=1")

    def test_migration_keeps_existing_sessions_runs_and_events(self):
        self.make_populated_v1_database()
        self.run_cycle(step=1)
        summary = read_history(self.database, self.profile)
        self.assertEqual((summary["runs"], summary["known_sessions"], summary["event_count"]), (2, 1, 1))
        self.assertEqual(len(self.telegram.calls), 1)
        with closing(sqlite3.connect(str(self.database) + ".before-v2.bak")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)

    def test_migration_failure_rolls_back_schema_and_preserves_v1_data(self):
        self.make_populated_v1_database()
        with patch("heimdall.storage.DELIVERY_SCHEMA", ("CREATE TABLE temporary_schema(value)", "INVALID SQL")):
            with self.assertRaises(HistoryError):
                History(self.database)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='temporary_schema'").fetchone()[0], 0)
        with History(self.database) as history:
            self.assertEqual(history.connection.execute("PRAGMA user_version").fetchone()[0], 2)

    def test_telegram_transport_and_bad_ack_are_uncertain_but_rejection_is_not(self):
        from tests.test_telegram import TOKEN, CHAT, Response
        opener = Mock()
        client = TelegramClient(TOKEN, opener=opener)
        for result in [URLError("offline"), None]:
            opener.open.side_effect = result
            opener.open.return_value = Response({"ok": True, "result": {}}, f"https://api.telegram.org/bot{TOKEN}/sendMessage")
            with self.assertRaises(TelegramError) as caught:
                client.send_text(CHAT, "test")
            self.assertTrue(caught.exception.uncertain)
        opener.open.side_effect = None
        opener.open.return_value = Response({"ok": False, "error_code": 429, "parameters": {"retry_after": 121}}, f"https://api.telegram.org/bot{TOKEN}/sendMessage", status=429)
        with self.assertRaises(TelegramError) as caught:
            client.send_text(CHAT, "test")
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(caught.exception.retry_after, 121)

    def test_windows_channel_is_not_supported(self):
        with self.assertRaises(DeliveryError):
            make_notifier("windows")

    def test_cli_check_without_flag_remains_read_and_record_only(self):
        with patch("heimdall.tracking.collect", return_value=Collection(self.snapshot(), {})), patch("heimdall.notifications.verify_and_notify") as notify, redirect_stdout(StringIO()):
            self.assertEqual(main(["verificar", "--banco", str(self.database)]), 0)
        notify.assert_not_called()

    def test_cli_history_and_manual_resolution_are_offline(self):
        self.telegram.failure = DeliveryError("uncertain", uncertain=True)
        self.run_cycle(channels=["telegram"])
        batch_id = self.telegram.calls[0]["id"]
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(main(["avisos", "historico", "--banco", str(self.database)]), 0)
            self.assertEqual(main(["avisos", "resolver", "--banco", str(self.database), "--lote", batch_id, "--acao", "confirmar"]), 0)
        self.assertIn("uncertain", output.getvalue())
        self.assertEqual(self.counts(), {("telegram", "sent"): 1})


if __name__ == "__main__":
    unittest.main()
