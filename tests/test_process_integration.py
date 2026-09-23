"""Persistência e recuperação após encerramento do processo."""

from contextlib import closing
from datetime import timedelta
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from heimdall.monitor import configure, set_feedback, set_paused
from tests._monitor_process_probe import BASE
from tests.test_rules import PROFILE_PATH, ROOT


class ProcessIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "control"
        self.database = self.directory / "history.sqlite3"
        configure(self.directory, PROFILE_PATH, self.database, 30, now=BASE - timedelta(minutes=2))
        set_paused(self.directory, False, now=BASE)
        set_feedback(self.directory, True, now=BASE)

    def run_process(self, mode, minutes, expected=0):
        result = subprocess.run(
            [sys.executable, "-m", "tests._monitor_process_probe", str(self.directory), mode, str(minutes)],
            cwd=ROOT, capture_output=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(result.returncode, expected, result.stderr.decode("utf-8", errors="replace"))

    def state(self):
        return json.loads((self.directory / "monitor-state.json").read_text(encoding="utf-8"))

    def calls(self, kind):
        rows = [json.loads(line) for line in (self.directory / "simulated-network.jsonl").read_text(encoding="utf-8").splitlines()]
        return [row for row in rows if row["kind"] == kind]

    def database_counts(self):
        with closing(sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True)) as connection:
            return {"sessions": connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
                    "events": connection.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                    "deliveries": dict(connection.execute("SELECT status, COUNT(*) FROM deliveries GROUP BY status")),
                    "integrity": connection.execute("PRAGMA quick_check").fetchone()[0]}

    def test_restart_offline_and_recovery_keep_history_without_duplicate_alerts(self):
        self.run_process("success", 0)
        self.assertEqual(self.database_counts(), {"sessions": 1, "events": 1, "deliveries": {"sent": 1}, "integrity": "ok"})
        self.assertEqual(self.state()["last_cycle_feedback"]["status"], "sent")
        self.run_process("success", 1)  # Processo novo respeita o intervalo salvo.
        self.assertEqual(len(self.calls("source")), 1)
        self.assertEqual(len(self.calls("feedback")), 1)
        self.run_process("source_error", 30, expected=1)
        self.assertIsNone(self.state()["cycle_summary"])
        self.assertIn("Sem resultado completo", self.calls("feedback")[-1]["text"])
        self.assertEqual(self.database_counts()["sessions"], 1)
        self.run_process("success", 60)
        self.assertEqual(self.state()["status"], "ok")
        self.assertEqual(self.state()["consecutive_failures"], 0)
        self.assertEqual(len(self.calls("source")), 3)
        self.assertEqual(len(self.calls("session")), 1)
        self.assertEqual(len(self.calls("feedback")), 3)
        self.assertEqual(self.database_counts(), {"sessions": 1, "events": 1, "deliveries": {"sent": 1}, "integrity": "ok"})

    def test_killed_sender_releases_locks_and_preserves_uncertain_delivery(self):
        self.run_process("crash_send", 0, expected=87)
        self.assertEqual(self.database_counts()["deliveries"], {"sending": 1})
        self.run_process("success", 1)
        self.assertEqual(self.state()["status"], "interrupted")
        self.assertEqual(len(self.calls("source")), 1)
        self.run_process("success", 30, expected=1)
        self.assertEqual(self.database_counts(), {"sessions": 1, "events": 1, "deliveries": {"uncertain": 1}, "integrity": "ok"})
        self.assertEqual(self.state()["last_error"], "delivery_attention")
        self.assertEqual(len(self.calls("source")), 2)
        self.assertEqual(len(self.calls("session")), 1)
        self.assertEqual(len(self.calls("feedback")), 1)
        self.assertIn("Incertos: 1", self.calls("feedback")[0]["text"])


if __name__ == "__main__":
    unittest.main()
