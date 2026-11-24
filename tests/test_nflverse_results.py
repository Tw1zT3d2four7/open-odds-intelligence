import unittest

from scripts.sync_nflverse_results import (
    parse_nflverse_games,
    records_to_csv,
    sync_nflverse_results,
)


HEADER = (
    "game_id,season,game_type,gameday,away_team,away_score,home_team,home_score,"
    "spread_line,total_line,home_moneyline,away_moneyline\n"
)


class NflverseResultsTests(unittest.TestCase):
    def test_reads_only_completed_regular_and_postseason_scores(self):
        content = HEADER + (
            "2022_01_BUF_LA,2022,REG,2022-09-08,BUF,31,LA,10,-2.5,52.5,-130,110\n"
            "2022_02_BUF_LA,2022,PRE,2022-08-20,BUF,20,LA,10,-2.5,52.5,-130,110\n"
            "2026_12_BUF_LA,2026,REG,2026-11-22,BUF,,,LA,,,,-\n"
        )
        rows = parse_nflverse_games(content, min_season=2022, through_season=2026)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["home_team"], "Los Angeles Rams")
        self.assertEqual(row["away_team"], "Buffalo Bills")
        self.assertEqual(row["home_score"], 10)
        self.assertEqual(row["away_score"], 31)
        self.assertEqual(row["commence_time"], "2022-09-08T12:00:00Z")
        self.assertEqual(row["source"], "nflverse schedules (CC BY 4.0)")
        self.assertNotIn("spread_line", row)
        self.assertNotIn("total_line", row)
        self.assertNotIn("home_moneyline", row)

    def test_maps_historical_franchise_abbreviations(self):
        content = HEADER + (
            "1999_01_SD_CIN,1999,REG,1999-09-19,CIN,7,SD,34,,, ,\n"
            "1999_01_OAK_GB,1999,REG,1999-09-12,OAK,24,GB,28,,, ,\n"
            "1999_01_WAS_DAL,1999,REG,1999-09-12,DAL,41,WAS,35,,, ,\n"
        )
        rows = parse_nflverse_games(content, min_season=1999, through_season=1999)
        by_id = {row["event_id"]: row for row in rows}
        self.assertEqual(by_id["1999_01_WAS_DAL"]["home_team"], "Washington Commanders")
        self.assertEqual(by_id["1999_01_WAS_DAL"]["away_team"], "Dallas Cowboys")
        self.assertEqual(by_id["1999_01_OAK_GB"]["away_team"], "Las Vegas Raiders")
        self.assertEqual(by_id["1999_01_SD_CIN"]["home_team"], "Los Angeles Chargers")

    def test_rejects_unknown_franchise_instead_of_silently_dropping_it(self):
        content = HEADER + (
            "2022_01_XYZ_BUF,2022,REG,2022-09-08,BUF,31,XYZ,10,,, ,\n"
        )
        with self.assertRaisesRegex(ValueError, "Unsupported nflverse team abbreviation"):
            parse_nflverse_games(content)

    def test_rejects_conflicting_duplicate_game_ids(self):
        content = HEADER + (
            "2022_01_BUF_LA,2022,REG,2022-09-08,BUF,31,LA,10,,, ,\n"
            "2022_01_BUF_LA,2022,REG,2022-09-08,BUF,30,LA,10,,, ,\n"
        )
        with self.assertRaisesRegex(ValueError, "Conflicting duplicate"):
            parse_nflverse_games(content)

    def test_csv_output_has_only_simulator_result_fields(self):
        rows = parse_nflverse_games(HEADER + (
            "2022_01_BUF_LA,2022,REG,2022-09-08,BUF,31,LA,10,-2.5,52.5,-130,110\n"
        ))
        csv_text = records_to_csv(rows)
        self.assertNotIn("spread_line", csv_text)
        self.assertNotIn("total_line", csv_text)
        self.assertNotIn("moneyline", csv_text)
        self.assertIn("event_id,sport_key,commence_time", csv_text)

    def test_dry_run_does_not_write_live_database(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from app.database import OddsDatabase

        with TemporaryDirectory() as temp:
            database = OddsDatabase(str(Path(temp) / "test.sqlite3"))
            content = HEADER + (
                "2022_01_BUF_LA,2022,REG,2022-09-08,BUF,31,LA,10,-2.5,52.5,-130,110\n"
            )
            result = sync_nflverse_results(database, content, apply=False)
            self.assertEqual(result["mode"], "dry_run")
            self.assertEqual(result["training_only_rows"], 1)
            self.assertEqual(database.simulation_settlements(), [])


    def test_walk_forward_model_uses_prior_seasons_and_keeps_holdout_separate(self):
        from scripts.sync_nflverse_results import build_nfl_historical_model

        rows = []
        strengths = {"Alpha": 18, "Bravo": 10, "Charlie": 2, "Delta": -8}
        pairs = [
            ("Alpha", "Bravo"), ("Alpha", "Charlie"), ("Alpha", "Delta"),
            ("Bravo", "Charlie"), ("Bravo", "Delta"), ("Charlie", "Delta"),
        ]
        for season in range(2000, 2014):
            for index, (home, away) in enumerate(pairs):
                home_score = 24 + strengths[home] - strengths[away] // 3 + (index % 3)
                away_score = 20 + strengths[away] - strengths[home] // 3 - (index % 2)
                rows.append({
                    "season": season,
                    "event_id": f"{season}-{index}",
                    "sport_key": "americanfootball_nfl",
                    "commence_time": f"{season}-10-{10 + index:02d}T12:00:00Z",
                    "home_team": home,
                    "away_team": away,
                    "home_score": max(0, home_score),
                    "away_score": max(0, away_score),
                    "status": "final",
                    "source": "fixture",
                })
        report, artifact = build_nfl_historical_model(
            rows,
            training_end_season=2007,
            calibration_end_season=2010,
            through_season=2013,
            min_calibration_games=1,
            min_holdout_games=1,
            min_team_games=2,
            trials=10_000,
        )
        self.assertEqual(report["walk_forward"]["training_games"], 48)
        self.assertEqual(report["walk_forward"]["calibration_games"], 18)
        self.assertEqual(report["walk_forward"]["holdout_games"], 18)
        self.assertEqual(report["calibration"]["sample_size"], 18)
        self.assertEqual(report["source"]["betting_fields_used"], [])
        if artifact is not None:
            self.assertEqual(artifact["validation"]["status"], "PASS")
            self.assertIn("independent_team_model", artifact["models"]["americanfootball_nfl"])

    def test_model_artifact_writer_refuses_failed_validation(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from scripts.sync_nflverse_results import write_model_artifact

        with TemporaryDirectory() as temp:
            target = Path(temp) / "models.json"
            with self.assertRaisesRegex(ValueError, "passing holdout validation"):
                write_model_artifact(target, {"validation": {"status": "BACKTEST FAILED - PICKS DISABLED"}})
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
