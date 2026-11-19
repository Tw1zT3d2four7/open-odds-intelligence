"""Paper-only evaluation helpers for stored, pregame simulator predictions.

This module deliberately does not fetch data or manufacture historical odds.
A prediction must have been generated from a real, timestamped pregame quote
snapshot; a settlement must come from an independently verified final score.
"""
from __future__ import annotations

import csv
import io
import math
from collections import defaultdict

REQUIRED_SETTLEMENT_FIELDS = {
    "event_id", "sport_key", "commence_time", "home_team", "away_team",
    "home_score", "away_score", "status", "source",
}


def parse_settlements_csv(content: str) -> list[dict]:
    """Parse strict final-score CSV rows; reject malformed or non-final results."""
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Settlement CSV is empty.")
    reader = csv.DictReader(io.StringIO(content))
    fields = set(reader.fieldnames or [])
    missing = sorted(REQUIRED_SETTLEMENT_FIELDS - fields)
    if missing:
        raise ValueError("Settlement CSV missing required columns: " + ", ".join(missing))
    records, seen = [], set()
    for line_number, raw in enumerate(reader, start=2):
        event_id = (raw.get("event_id") or "").strip()
        status = (raw.get("status") or "").strip().lower()
        source = (raw.get("source") or "").strip()
        if not event_id:
            raise ValueError(f"Line {line_number}: event_id is required.")
        if event_id in seen:
            raise ValueError(f"Line {line_number}: duplicate event_id {event_id!r}.")
        seen.add(event_id)
        if status != "final":
            raise ValueError(f"Line {line_number}: only verified final results may be imported.")
        if not source:
            raise ValueError(f"Line {line_number}: settlement source is required.")
        try:
            home_score = float(raw["home_score"])
            away_score = float(raw["away_score"])
        except (TypeError, ValueError):
            raise ValueError(f"Line {line_number}: scores must be numeric.") from None
        if (not math.isfinite(home_score) or not math.isfinite(away_score)
                or home_score < 0 or away_score < 0
                or not home_score.is_integer() or not away_score.is_integer()):
            raise ValueError(f"Line {line_number}: scores must be finite non-negative integers.")
        record = {key: (raw.get(key) or "").strip() for key in REQUIRED_SETTLEMENT_FIELDS}
        record["home_score"] = int(home_score)
        record["away_score"] = int(away_score)
        records.append(record)
    return records


def score_prediction(prediction: dict, settlement: dict) -> dict:
    """Score one simulated forecast against one independently imported result."""
    if str(prediction.get("event_id")) != str(settlement.get("event_id")):
        raise ValueError("Prediction and settlement event IDs do not match.")
    probs = prediction.get("win_probabilities") or {}
    home = float(probs.get("home", -1)) / 100
    away = float(probs.get("away", -1)) / 100
    tie = float(probs.get("tie", -1)) / 100
    values = (home, away, tie)
    if any(not math.isfinite(x) or x < 0 or x > 1 for x in values):
        raise ValueError("Prediction contains invalid win probabilities.")
    if abs(sum(values) - 1) > 0.005:
        raise ValueError("Win probabilities must sum to 100%.")
    hs, aws = int(settlement["home_score"]), int(settlement["away_score"])
    actual = (1.0, 0.0, 0.0) if hs > aws else (0.0, 1.0, 0.0) if hs < aws else (0.0, 0.0, 1.0)
    brier = sum((p - y) ** 2 for p, y in zip(values, actual))
    eps = 1e-12
    actual_index = actual.index(1.0)
    log_loss = -math.log(max(eps, values[actual_index]))
    projected = prediction.get("projected_score") or {}
    projected_home = float(projected.get("home", float("nan")))
    projected_away = float(projected.get("away", float("nan")))
    if not math.isfinite(projected_home) or not math.isfinite(projected_away):
        raise ValueError("Prediction contains invalid projected scores.")
    return {
        "event_id": str(settlement["event_id"]),
        "sport": str(settlement.get("sport_key") or prediction.get("sport") or ""),
        "actual_result": "home" if hs > aws else "away" if hs < aws else "tie",
        "predicted_home_probability": home,
        "brier_score": brier,
        "log_loss": log_loss,
        "home_score_error": abs(projected_home - hs),
        "away_score_error": abs(projected_away - aws),
        "total_score_error": abs(projected_home + projected_away - hs - aws),
        "margin_error": abs((projected_home - projected_away) - (hs - aws)),
    }


def _mean(values):
    return sum(values) / len(values) if values else None


def evaluate_predictions(rows: list[dict], calibration_bins: int = 10) -> dict:
    """Aggregate proper scoring rules, score errors and probability calibration."""
    if not isinstance(calibration_bins, int) or isinstance(calibration_bins, bool) or not 2 <= calibration_bins <= 20:
        raise ValueError("calibration_bins must be an integer from 2 through 20.")
    if not rows:
        return {"sample_size": 0, "status": "INSUFFICIENT_DATA", "by_sport": {}, "calibration": []}
    by_sport = defaultdict(list)
    for row in rows:
        by_sport[row.get("sport") or "unknown"].append(row)

    def summarize(items):
        return {
            "sample_size": len(items),
            "brier_score": _mean([x["brier_score"] for x in items]),
            "log_loss": _mean([x["log_loss"] for x in items]),
            "home_score_mae": _mean([x["home_score_error"] for x in items]),
            "away_score_mae": _mean([x["away_score_error"] for x in items]),
            "total_score_mae": _mean([x["total_score_error"] for x in items]),
            "margin_mae": _mean([x["margin_error"] for x in items]),
        }

    calibration = []
    for index in range(calibration_bins):
        low, high = index / calibration_bins, (index + 1) / calibration_bins
        bucket = [x for x in rows if low <= x["predicted_home_probability"] < high
                  or (index == calibration_bins - 1 and x["predicted_home_probability"] == 1)]
        if bucket:
            observed = sum(x["actual_result"] == "home" for x in bucket) / len(bucket)
            predicted = _mean([x["predicted_home_probability"] for x in bucket])
            calibration.append({
                "lower_bound": round(low, 3), "upper_bound": round(high, 3),
                "sample_size": len(bucket), "mean_predicted": predicted,
                "observed_home_win_rate": observed,
                "absolute_calibration_error": abs(predicted - observed),
            })
    return {
        "sample_size": len(rows),
        "status": "DESCRIPTIVE_ONLY" if len(rows) < 200 else "EVALUATION_AVAILABLE",
        "overall": summarize(rows),
        "by_sport": {sport: summarize(items) for sport, items in sorted(by_sport.items())},
        "calibration": calibration,
        "interpretation": (
            "Paper-only historical evaluation. Metrics describe this sample and do not prove "
            "future profitability. Use chronological holdouts; do not tune on the reported test set."
        ),
    }


def _parse_timestamp(value):
    from datetime import datetime, timezone
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ValueError(f"Invalid ISO-8601 timestamp: {value!r}") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalized_team(value):
    return "".join(ch.lower() for ch in str(value or "") if ch.isalnum())


def _load_latest_pregame_event(database, settlement):
    """Rebuild one event from its latest archived snapshot strictly before start."""
    commence = _parse_timestamp(settlement["commence_time"])
    with database.connect() as db:
        rows = db.execute(
            """SELECT s.id AS snapshot_id, s.fetched_at,
                      q.event_id, q.sport, q.sport_title, q.commence_time,
                      q.home_team, q.away_team, q.bookmaker_key,
                      q.bookmaker_title, q.market_key, q.outcome_name,
                      q.outcome_description, q.point, q.price
               FROM quotes q JOIN snapshots s ON s.id=q.snapshot_id
               WHERE q.event_id=? AND q.sport=?
               ORDER BY s.fetched_at ASC, s.id ASC, q.bookmaker_key""",
            (str(settlement["event_id"]), str(settlement["sport_key"])),
        ).fetchall()
    eligible = []
    for row in rows:
        try:
            observed = _parse_timestamp(row["fetched_at"])
        except ValueError:
            continue
        if observed < commence:
            eligible.append((observed, row))
    if not eligible:
        return None, "no archived quote snapshot strictly before game start"
    latest_time = max(item[0] for item in eligible)
    latest_rows = [row for observed, row in eligible if observed == latest_time]
    snapshot_ids = {row["snapshot_id"] for row in latest_rows}
    if len(snapshot_ids) != 1:
        return None, "ambiguous pregame snapshot identity"
    first = latest_rows[0]
    if (_normalized_team(first["home_team"]) != _normalized_team(settlement["home_team"])
            or _normalized_team(first["away_team"]) != _normalized_team(settlement["away_team"])):
        return None, "settlement home/away teams do not match archived event"
    event = {
        "id": str(settlement["event_id"]), "sport_key": str(settlement["sport_key"]),
        "sport_title": first["sport_title"], "commence_time": first["commence_time"],
        "home_team": first["home_team"], "away_team": first["away_team"],
        "bookmakers": [],
    }
    books = {}
    for row in latest_rows:
        book = books.get(row["bookmaker_key"])
        if book is None:
            book = {"key": row["bookmaker_key"], "title": row["bookmaker_title"], "markets": []}
            books[row["bookmaker_key"]] = book
            event["bookmakers"].append(book)
        markets = {market["key"]: market for market in book["markets"]}
        market = markets.get(row["market_key"])
        if market is None:
            market = {"key": row["market_key"], "outcomes": []}
            book["markets"].append(market)
        outcome = {"name": row["outcome_name"], "price": row["price"]}
        if row["point"] is not None:
            outcome["point"] = row["point"]
        if row["outcome_description"]:
            outcome["description"] = row["outcome_description"]
        market["outcomes"].append(outcome)
    return event, None


def run_historical_backtest(database, settlements: list[dict], seed_salt: str = "ooi-sim-v1") -> dict:
    """Replay the latest real pregame snapshot per settled event; never fetch odds."""
    import hashlib
    from .simulation import run_game_simulation

    scored, skipped = [], []
    for settlement in settlements:
        event, reason = _load_latest_pregame_event(database, settlement)
        if reason:
            skipped.append({"event_id": settlement.get("event_id"), "reason": reason})
            continue
        seed_material = f"{seed_salt}:{settlement['event_id']}".encode("utf-8")
        seed = int.from_bytes(hashlib.sha256(seed_material).digest()[:4], "big")
        try:
            prediction = run_game_simulation(event, seed=seed)
            scored.append(score_prediction(prediction, settlement))
        except (ValueError, TypeError, KeyError) as exc:
            skipped.append({"event_id": settlement.get("event_id"), "reason": str(exc)})
    report = evaluate_predictions(scored)
    report["settlements_submitted"] = len(settlements)
    report["events_scored"] = len(scored)
    report["events_skipped"] = len(skipped)
    report["skipped"] = skipped
    report["data_policy"] = (
        "Uses only the latest archived sportsbook snapshot strictly before each event's "
        "commence_time. No provider requests, current odds, post-start quotes, or synthetic "
        "historical prices are used. Events without a valid archived spread and total are skipped."
    )
    return report
