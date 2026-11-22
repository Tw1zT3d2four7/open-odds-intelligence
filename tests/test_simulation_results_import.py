import csv
import io
import tempfile
import unittest
from pathlib import Path

from app.database import OddsDatabase
from scripts.import_simulation_results import export_archived_event_template, import_results


class SimulationResultsImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = OddsDatabase(str(Path(self.temp.name) / "odds.sqlite3"))
        self.db.save_snapshot("basketball_nba", [{
            "id": "evt-1", "sport_key": "basketball_nba",
            "commence_time": "2026-01-01T00:00:00Z",
            "home_team": "Home Team", "away_team": "Away Team",
            "bookmakers": [{"key": "book", "markets": [{
                "key": "h2h", "outcomes": [
                    {"name": "Home Team", "price": -110},
                    {"name": "Away Team", "price": -110},
                ],
            }]}],
        }], provider="test")

    def tearDown(self):
        self.temp.cleanup()

    def test_template_exports_exact_archived_identity_with_blank_result(self):
        path = Path(self.temp.name) / "template.csv"
        count = export_archived_event_template(self.db, str(path))
        self.assertEqual(count, 1)
        with path.open(encoding="utf-8", newline="") as handle:
            row = next(csv.DictReader(handle))
        self.assertEqual(row["event_id"], "evt-1")
        self.assertEqual(row["sport_key"], "basketball_nba")
        self.assertEqual(row["home_team"], "Home Team")
        self.assertEqual(row["home_score"], "")

    def _csv(self, event_id="evt-1", home="Home Team"):
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=[
            "event_id", "sport_key", "commence_time", "home_team", "away_team",
            "home_score", "away_score", "status", "source",
        ])
        writer.writeheader()
        writer.writerow({
            "event_id": event_id, "sport_key": "basketball_nba",
            "commence_time": "2026-01-01T00:00:00Z",
            "home_team": home, "away_team": "Away Team",
            "home_score": 101, "away_score": 98, "status": "final",
            "source": "official-results",
        })
        return output.getvalue()

    def test_dry_run_validates_without_persisting(self):
        result = import_results(self.db, self._csv(), apply=False)
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(result["mode"], "dry_run")
        self.assertEqual(self.db.simulation_settlements(), [])

    def test_apply_persists_verified_matching_result(self):
        result = import_results(self.db, self._csv(), apply=True)
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(len(self.db.simulation_settlements()), 1)

    def test_historical_training_only_import_accepts_unarchived_event(self):
        result = import_results(
            self.db, self._csv(event_id="historical-event-1"),
            apply=True, training_only=True,
        )
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(result["dataset"], "training_only")
        stored = self.db.simulation_settlements()
        self.assertEqual(len(stored), 1)
        self.assertTrue(stored[0]["training_only"])
        self.assertEqual(stored[0]["event_id"], "historical-event-1")

    def test_training_only_dry_run_does_not_persist(self):
        result = import_results(
            self.db, self._csv(event_id="historical-event-2"),
            apply=False, training_only=True,
        )
        self.assertEqual(result["inserted"], 1)
        self.assertEqual(result["mode"], "dry_run")
        self.assertEqual(self.db.simulation_settlements(), [])

    def test_mismatched_team_is_rejected(self):
        result = import_results(self.db, self._csv(home="Wrong Team"), apply=False)
        self.assertEqual(result["inserted"], 0)
        self.assertEqual(result["rejected"][0]["reason"], "result home/away teams do not match archived event")


if __name__ == "__main__":
    unittest.main()
