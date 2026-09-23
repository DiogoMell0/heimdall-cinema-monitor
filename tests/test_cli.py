"""Captura padrão e isolamento dos dados locais."""
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import unittest
from unittest.mock import patch

from heimdall.cli import main
from heimdall.monitor import default_directory
from heimdall.storage import default_database_path
from heimdall.telegram_setup import credentials_path


class CliTests(unittest.TestCase):
    def test_default_example_uses_the_historical_capture_offline(self):
        output = StringIO()
        with patch("heimdall.cli.collect") as online, redirect_stdout(output):
            self.assertEqual(main(["analisar", "--listar"]), 0)
        online.assert_not_called()
        self.assertIn("137 | Compatíveis: 0", output.getvalue())
        self.assertIn("86524297", output.getvalue())
        self.assertIn("sem Legendado", output.getvalue())
        self.assertIn("sem Infinity Vision", output.getvalue())

    def test_synthetic_fixture_exercises_a_matching_session(self):
        fixture = Path(__file__).parent / "fixtures/sessao-compativel.json"
        output = StringIO()
        with patch("heimdall.cli.collect") as online, redirect_stdout(output):
            self.assertEqual(main(["analisar", "--arquivo", str(fixture), "--listar"]), 0)
        online.assert_not_called()
        self.assertIn("3 | Compatíveis: 1", output.getvalue())
        self.assertIn("demo-legendado", output.getvalue())
        self.assertIn("sem Legendado", output.getvalue())
        self.assertIn("sem Infinity Vision", output.getvalue())

    def test_default_paths_share_the_application_directory(self):
        home = Path("fictional-user").resolve()
        with patch("pathlib.Path.home", return_value=home):
            root = home / ".heimdall-cinema-monitor"
            self.assertEqual(default_directory(), root)
            self.assertEqual(default_database_path(), root / "heimdall.sqlite3")
            self.assertEqual(credentials_path(), root / "telegram.secret")


if __name__ == "__main__":
    unittest.main()
