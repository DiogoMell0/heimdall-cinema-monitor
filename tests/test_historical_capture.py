"""Regressão offline baseada na API e na observação do site em 07/09/2026."""
from datetime import date
import json
from pathlib import Path
import unittest

from heimdall.config import load_profile
from heimdall.rules import rejection_reasons
from heimdall.sources.snapshot import load_snapshot

ROOT = Path(__file__).resolve().parents[1]
HISTORY = ROOT / "examples/historico"


class HistoricalCaptureTests(unittest.TestCase):
    def test_real_capture_preserves_observed_counts_and_rejections(self):
        profile = load_profile(ROOT / "config/perfil.toml")
        snapshot = load_snapshot(HISTORY / "api-2026-09-07.json", profile)
        self.assertEqual(len(snapshot.sessions), 137)
        self.assertEqual(len(snapshot.published_dates), 7)
        self.assertNotIn(date(2026, 10, 1), snapshot.published_dates)
        self.assertEqual(sum("Infinity Vision" in s.labels for s in snapshot.sessions), 39)
        self.assertEqual(sum("Legendado" in s.labels for s in snapshot.sessions), 35)
        self.assertTrue(all(rejection_reasons(s, profile, now=snapshot.captured_at) for s in snapshot.sessions))
        sessions = {s.id: s for s in snapshot.sessions}
        self.assertIn("sem Legendado", rejection_reasons(sessions["86524297"], profile, now=snapshot.captured_at))
        self.assertIn("sem Infinity Vision", rejection_reasons(sessions["86538471"], profile, now=snapshot.captured_at))

    def test_api_agrees_with_39_independent_historical_site_observations(self):
        api = json.loads((HISTORY / "api-2026-09-07.json").read_text(encoding="utf-8"))
        site = json.loads((HISTORY / "site-2026-09-07.json").read_text(encoding="utf-8"))
        actual = {}
        for day in api["data"]:
            for theater in day["theaters"]:
                for room in theater["rooms"]:
                    for session in room["sessions"]:
                        actual[session["id"]] = (day["date"], theater["name"], session["date"]["localDate"][11:16],
                                                   {label["name"] for label in session["types"] if label["display"]})
        self.assertEqual([day["date"] for day in api["data"]], site["availableDates"])
        checked = 0
        for day in site["samples"]:
            for cinema in day["cinemas"]:
                for session_id, time, labels in cinema["rows"]:
                    with self.subTest(session_id=session_id):
                        self.assertEqual(actual[session_id], (day["date"], cinema["name"], time, set(labels)))
                    checked += 1
        self.assertEqual(checked, 39)


if __name__ == "__main__":
    unittest.main()
