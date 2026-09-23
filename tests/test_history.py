"""Persistência, comparação, transações e comandos, em bancos temporários."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta
from io import StringIO
import json
from pathlib import Path
import sqlite3
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch

from heimdall.cli import main
from heimdall.config import load_profile
from heimdall.models import Snapshot, ValidationError
from heimdall.sources.http import Collection, CollectionError
from heimdall.sources.ingresso import parse_sessions
from heimdall.storage import History, HistoryError, default_database_path, profile_key, read_history
from heimdall.tracking import check_online, register_capture
from tests.test_rules import PROFILE_PATH, ROOT, synthetic_payload

NOW = datetime.fromisoformat("2026-09-14T15:00:00-03:00")


class HistoryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.database = self.folder / "history.sqlite3"
        self.profile = load_profile(PROFILE_PATH)
        _, sessions = parse_sessions(synthetic_payload(), movie_id=self.profile.movie_id, city_id=self.profile.city_id)
        self.matching = sessions[0]
        self.dubbed = replace(self.matching, labels=frozenset({"Infinity Vision", "Dublado"}))

    def snapshot(self, *sessions, step=0):
        return Snapshot(NOW + timedelta(minutes=step), tuple(sorted({row.programming_date for row in sessions})), tuple(sessions))

    def test_first_capture_records_all_sessions_and_only_matching_events(self):
        snapshot = self.snapshot(self.matching, replace(self.dubbed, id="dub-2"))
        with History(self.database, dataset="replay") as history:
            change = history.record_success(snapshot, self.profile)
            summary = history.summary(self.profile)
        self.assertEqual(change.new_sessions, 2)
        self.assertEqual(change.changed_sessions, 0)
        self.assertEqual(len(change.new_events), 1)
        self.assertEqual(change.new_events[0]["kind"], "new_matching_session")
        self.assertEqual(summary["known_sessions"], 2)
        self.assertEqual(summary["last_success"]["matching_count"], 1)

    def test_repeat_after_reopen_does_not_duplicate_and_updates_last_seen(self):
        with History(self.database, dataset="replay") as history:
            first = history.record_success(self.snapshot(self.matching), self.profile)
        with History(self.database, dataset="replay") as history:
            second = history.record_success(self.snapshot(self.matching, step=1), self.profile)
            row = history.connection.execute("SELECT first_seen_at, last_seen_at FROM sessions").fetchone()
            summary = history.summary(self.profile)
        self.assertEqual(len(first.new_events), 1)
        self.assertEqual(second.new_sessions, 0)
        self.assertEqual(second.changed_sessions, 0)
        self.assertEqual(second.new_events, ())
        self.assertNotEqual(row["first_seen_at"], row["last_seen_at"])
        self.assertEqual(summary["event_count"], 1)
        self.assertEqual(summary["runs"], 2)

    def test_known_dubbed_becomes_matching_once(self):
        with History(self.database, dataset="replay") as history:
            history.record_success(self.snapshot(self.dubbed), self.profile)
            change = history.record_success(self.snapshot(self.matching, step=1), self.profile)
            repeat = history.record_success(self.snapshot(self.matching, step=2), self.profile)
        self.assertEqual(change.new_sessions, 0)
        self.assertEqual(change.changed_sessions, 1)
        self.assertEqual(change.new_events[0]["kind"], "became_matching")
        self.assertEqual(repeat.new_events, ())

    def test_matching_incompatible_matching_is_a_second_real_transition(self):
        with History(self.database, dataset="replay") as history:
            history.record_success(self.snapshot(self.matching), self.profile)
            history.record_success(self.snapshot(self.dubbed, step=1), self.profile)
            change = history.record_success(self.snapshot(self.matching, step=2), self.profile)
            summary = history.summary(self.profile)
        self.assertEqual(len(change.new_events), 1)
        self.assertEqual(summary["event_count"], 2)
        self.assertEqual(summary["events"][0]["eligibility_version"], 2)

    def test_missing_session_is_preserved_and_reappearance_is_not_a_new_event(self):
        with History(self.database, dataset="replay") as history:
            history.record_success(self.snapshot(self.matching), self.profile)
            history.record_success(self.snapshot(step=1), self.profile)
            middle = history.summary(self.profile)
            returned = history.record_success(self.snapshot(self.matching, step=2), self.profile)
        self.assertEqual(middle["last_success"]["session_count"], 0)
        self.assertEqual(middle["known_sessions"], 1)
        self.assertEqual(returned.new_events, ())
        self.assertEqual(returned.new_sessions, 0)

    def test_schedule_change_updates_session_but_preserves_event_snapshot(self):
        later = replace(self.matching, starts_at=self.matching.starts_at + timedelta(hours=1))
        with History(self.database, dataset="replay") as history:
            history.record_success(self.snapshot(self.matching), self.profile)
            change = history.record_success(self.snapshot(later, step=1), self.profile)
            saved = json.loads(history.connection.execute("SELECT payload_json FROM sessions").fetchone()[0])
            event = history.summary(self.profile)["events"][0]
        self.assertEqual(change.changed_sessions, 1)
        self.assertEqual(change.new_events, ())
        self.assertEqual(saved["starts_at"], later.starts_at.isoformat())
        self.assertEqual(event["payload"]["starts_at"], self.matching.starts_at.isoformat())

    def test_label_case_and_order_are_not_changes(self):
        alternate = replace(self.matching, labels=frozenset({"LEGENDADO", " infinity vision "}))
        with History(self.database, dataset="replay") as history:
            history.record_success(self.snapshot(self.matching), self.profile)
            change = history.record_success(self.snapshot(alternate, step=1), self.profile)
        self.assertEqual(change.changed_sessions, 0)
        self.assertEqual(change.new_events, ())

    def test_network_failure_records_attempt_without_touching_last_success(self):
        collection = Collection(self.snapshot(self.matching), {})
        with patch("heimdall.tracking.collect", return_value=collection):
            check_online(self.profile, self.database)
        before = read_history(self.database, self.profile)
        with patch("heimdall.tracking.collect", side_effect=CollectionError("timeout", "Teste de timeout")), self.assertRaises(CollectionError):
            check_online(self.profile, self.database)
        after = read_history(self.database, self.profile)
        self.assertEqual(after["known_sessions"], 1)
        self.assertEqual(after["event_count"], 1)
        self.assertEqual(after["last_success"], before["last_success"])
        self.assertEqual(after["failures"], 1)
        self.assertEqual(after["latest_run"]["error_category"], "timeout")
        self.assertIsNone(after["latest_run"]["session_count"])

    def test_malformed_capture_records_failure_without_changing_sessions(self):
        valid = ROOT / "tests/fixtures/programacao-atualizada.json"
        register_capture(self.profile, valid, self.database)
        invalid = self.folder / "broken.json"
        invalid.write_text('{"data":', encoding="utf-8")
        with self.assertRaises(ValidationError):
            register_capture(self.profile, invalid, self.database)
        summary = read_history(self.database, self.profile)
        self.assertEqual(summary["known_sessions"], 144)
        self.assertEqual(summary["last_success"]["session_count"], 144)
        self.assertEqual(summary["failures"], 1)

    def test_transaction_rolls_back_session_and_run_if_event_insert_fails(self):
        with History(self.database, dataset="replay") as history:
            history.record_success(self.snapshot(self.dubbed), self.profile)
            history.connection.execute("""CREATE TRIGGER simulated_failure BEFORE INSERT ON events
                                       BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END""")
            with self.assertRaises(HistoryError):
                history.record_success(self.snapshot(self.matching, step=1), self.profile)
            summary = history.summary(self.profile)
            row = history.connection.execute("SELECT eligible, eligibility_version FROM sessions").fetchone()
            self.assertEqual(summary["runs"], 1)
            self.assertEqual(summary["event_count"], 0)
            self.assertEqual(tuple(row), (0, 0))
            history.connection.execute("DROP TRIGGER simulated_failure")
            recovered = history.record_success(self.snapshot(self.matching, step=1), self.profile)
        self.assertEqual(len(recovered.new_events), 1)

    def test_duplicate_ids_rejected_before_writing(self):
        with History(self.database, dataset="replay") as history:
            with self.assertRaises(ValidationError):
                history.record_success(self.snapshot(self.matching, self.matching), self.profile)
            self.assertEqual(history.summary(self.profile)["runs"], 0)

    def test_capture_with_wrong_identity_is_rejected(self):
        with History(self.database, dataset="replay") as history:
            with self.assertRaises(ValidationError):
                history.record_success(self.snapshot(replace(self.matching, city_id="outra")), self.profile)
            self.assertEqual(history.summary(self.profile)["known_sessions"], 0)

    def test_older_capture_and_conflicting_same_timestamp_do_not_overwrite(self):
        with History(self.database, dataset="replay") as history:
            history.record_success(self.snapshot(self.matching, step=2), self.profile)
            for snapshot in (self.snapshot(self.dubbed, step=1), self.snapshot(self.dubbed, step=2)):
                with self.subTest(instant=snapshot.captured_at), self.assertRaises(ValidationError):
                    history.record_success(snapshot, self.profile)
            summary = history.summary(self.profile)
        self.assertEqual(summary["runs"], 1)
        self.assertEqual(summary["last_success"]["matching_count"], 1)

    def test_filters_are_separate_scopes_and_display_names_do_not_change_scope(self):
        dubbed_profile = replace(self.profile, language="Dublado")
        renamed = replace(self.profile, movie_name="Nome de exibição", certification=" INFINITY VISION ")
        self.assertEqual(profile_key(self.profile), profile_key(renamed))
        with History(self.database, dataset="replay") as history:
            history.record_success(self.snapshot(self.matching), self.profile)
            change = history.record_success(self.snapshot(self.dubbed, step=1), dubbed_profile)
            repeat = history.record_success(self.snapshot(self.matching, step=1), renamed)
            original = history.summary(self.profile)
            dubbed = history.summary(dubbed_profile)
        self.assertEqual(len(change.new_events), 1)
        self.assertEqual(repeat.new_events, ())
        self.assertEqual(original["event_count"], 1)
        self.assertEqual(dubbed["event_count"], 1)

    def test_two_writers_do_not_duplicate_event(self):
        with History(self.database, dataset="replay"):
            pass
        barrier = Barrier(2)

        def record():
            with History(self.database, dataset="replay") as history:
                barrier.wait(timeout=5)
                return history.record_success(self.snapshot(self.matching), self.profile)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(record), pool.submit(record)]
            results = [future.result(timeout=10) for future in futures]
        self.assertEqual(sum(len(result.new_events) for result in results), 1)
        self.assertEqual(read_history(self.database, self.profile)["runs"], 2)

    def test_replay_and_online_cannot_share_database(self):
        with History(self.database, dataset="replay"):
            pass
        with patch("heimdall.tracking.collect") as collector, self.assertRaises(HistoryError):
            check_online(self.profile, self.database)
        collector.assert_not_called()
        self.assertEqual(read_history(self.database, self.profile)["runs"], 0)

    def test_unrecognized_database_is_not_modified(self):
        connection = sqlite3.connect(self.database)
        connection.execute("CREATE TABLE personal_notes(note TEXT)")
        connection.execute("INSERT INTO personal_notes VALUES ('preserve')")
        connection.commit()
        connection.close()
        with self.assertRaises(HistoryError):
            History(self.database)
        connection = sqlite3.connect(self.database)
        self.assertEqual(connection.execute("SELECT note FROM personal_notes").fetchone()[0], "preserve")
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0], 1)
        connection.close()

    def test_read_does_not_create_missing_database_or_directories(self):
        path = self.folder / "missing" / "history.sqlite3"
        self.assertIsNone(read_history(path, self.profile))
        self.assertFalse(path.parent.exists())

    def test_fixtures_find_seven_new_ids_and_replay_is_idempotent(self):
        first = ROOT / "tests/fixtures/programacao-inicial.json"
        second = ROOT / "tests/fixtures/programacao-atualizada.json"
        initial = register_capture(self.profile, first, self.database)
        updated = register_capture(self.profile, second, self.database)
        repeated = register_capture(self.profile, second, self.database)
        self.assertEqual(initial.changes.new_sessions, 137)
        self.assertEqual(updated.changes.new_sessions, 7)
        self.assertEqual(updated.changes.new_events, ())
        self.assertEqual(repeated.changes.new_sessions, 0)
        self.assertEqual(repeated.changes.changed_sessions, 0)
        self.assertEqual(repeated.changes.new_events, ())
        self.assertEqual(read_history(self.database, self.profile)["known_sessions"], 144)

    def test_sql_values_with_quotes_do_not_change_schema(self):
        odd = replace(self.matching, id="session'); DROP TABLE sessions; --", theater_name="Cine D'Ávila")
        with History(self.database, dataset="replay") as history:
            change = history.record_success(self.snapshot(odd), self.profile)
            summary = history.summary(self.profile)
        self.assertEqual(change.new_sessions, 1)
        self.assertEqual(summary["events"][0]["payload"]["theater_name"], "Cine D'Ávila")

    def test_cli_offline_records_and_history_reopens(self):
        output = StringIO()
        capture = ROOT / "tests/fixtures/programacao-atualizada.json"
        with redirect_stdout(output):
            first = main(["registrar", "--arquivo", str(capture), "--banco", str(self.database)])
            second = main(["historico", "--banco", str(self.database)])
        self.assertEqual((first, second), (0, 0))
        self.assertIn("OFFLINE", output.getvalue())
        self.assertIn("Sessões novas no histórico: 144", output.getvalue())
        self.assertIn("Sessões conhecidas: 144", output.getvalue())

    def test_cli_online_failure_is_visible_in_history(self):
        with patch("heimdall.tracking.collect", side_effect=CollectionError("timeout", "Timeout sintético")), redirect_stderr(StringIO()):
            code = main(["verificar", "--banco", str(self.database)])
        output = StringIO()
        with redirect_stdout(output):
            history_code = main(["historico", "--banco", str(self.database)])
        self.assertEqual((code, history_code), (1, 0))
        self.assertIn("Falhas: 1", output.getvalue())
        self.assertIn("Timeout sintético", output.getvalue())

    def test_default_path_is_stable_across_terminals_and_history_limit_is_checked(self):
        with patch("heimdall.storage.Path.home", return_value=self.folder), \
                patch.dict("os.environ", {"LOCALAPPDATA": str(self.folder / "virtualized")}):
            self.assertEqual(default_database_path(), self.folder / ".heimdall-cinema-monitor" / "heimdall.sqlite3")
        with redirect_stderr(StringIO()):
            code = main(["historico", "--banco", str(self.database), "--limite", "0"])
        self.assertEqual(code, 1)
        self.assertFalse(self.database.exists())
