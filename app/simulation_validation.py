"""Paper-only evaluation helpers for stored, pregame simulator predictions.

This module deliberately does not fetch data or manufacture historical odds.
A prediction must have been generated from a real, timestamped pregame quote
snapshot; a settlement must come from an independently verified final score.
"""
from __future__ import annotations

import csv
import io
import math
import random
from collections import defaultdict
from statistics import median

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
        for field in ("sport_key", "commence_time", "home_team", "away_team"):
            if not (raw.get(field) or "").strip():
                raise ValueError(f"Line {line_number}: {field} is required.")
        try:
            _parse_timestamp(raw["commence_time"])
        except ValueError:
            raise ValueError(f"Line {line_number}: commence_time must be ISO-8601 with timezone.") from None
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
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f"Line {line_number}: scores must be numeric.") from None
        if (not math.isfinite(home_score) or not math.isfinite(away_score)
                or home_score < 0 or away_score < 0
                or not home_score.is_integer() or not away_score.is_integer()):
            raise ValueError(f"Line {line_number}: scores must be finite non-negative integers.")
        record = {key: (raw.get(key) or "").strip() for key in REQUIRED_SETTLEMENT_FIELDS}
        record["home_score"] = int(home_score)
        record["away_score"] = int(away_score)
        records.append(record)
    if not records:
        raise ValueError("Settlement CSV contains no result rows.")
    return records


def score_prediction(prediction: dict, settlement: dict) -> dict:
    """Score one simulated forecast against one independently imported result."""
    if not isinstance(prediction, dict) or not isinstance(settlement, dict):
        raise ValueError("Prediction and settlement must be objects.")

    def stable_event_id(value):
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            return None
        value = str(value).strip()
        return value or None

    prediction_event_id = stable_event_id(prediction.get("event_id"))
    settlement_event_id = stable_event_id(settlement.get("event_id"))
    if prediction_event_id is None or settlement_event_id is None:
        raise ValueError("Prediction and settlement must include stable event IDs.")
    if prediction_event_id != settlement_event_id:
        raise ValueError("Prediction and settlement event IDs do not match.")
    prediction_sport = str(prediction.get("sport") or "")
    settlement_sport = str(settlement.get("sport_key") or "")
    if prediction_sport and settlement_sport and prediction_sport != settlement_sport:
        raise ValueError("Prediction and settlement leagues do not match.")

    def finite_number(value, label):
        if isinstance(value, bool):
            raise ValueError(f"{label} must be numeric, not boolean.")
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f"{label} must be numeric.") from None
        if not math.isfinite(number):
            raise ValueError(f"{label} must be finite.")
        return number

    def final_score(value, label):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{label} must be a non-negative whole-number integer.")
        return value

    hs = final_score(settlement.get("home_score"), "Home score")
    aws = final_score(settlement.get("away_score"), "Away score")
    probs = prediction.get("win_probabilities")
    if not isinstance(probs, dict):
        raise ValueError("Prediction win probabilities must be an object.")
    home = finite_number(probs.get("home", -1), "Home win probability") / 100
    away = finite_number(probs.get("away", -1), "Away win probability") / 100
    tie = finite_number(probs.get("tie", -1), "Tie probability") / 100
    values = (home, away, tie)
    if any(not math.isfinite(x) or x < 0 or x > 1 for x in values):
        raise ValueError("Prediction contains invalid win probabilities.")
    if abs(sum(values) - 1) > 0.005:
        raise ValueError("Win probabilities must sum to 100%.")
    actual = (1.0, 0.0, 0.0) if hs > aws else (0.0, 1.0, 0.0) if hs < aws else (0.0, 0.0, 1.0)
    brier = sum((p - y) ** 2 for p, y in zip(values, actual))
    eps = 1e-12
    actual_index = actual.index(1.0)
    log_loss = -math.log(max(eps, values[actual_index]))
    total_market = prediction.get("total_market")
    if not isinstance(total_market, dict):
        raise ValueError("Prediction total market must be an object.")
    total_line = finite_number(total_market.get("consensus_line", float("nan")), "Consensus total")
    total_probs = tuple(finite_number(total_market.get(key, -1), f"{key.title()} probability") / 100
                        for key in ("over", "under", "push"))
    if not math.isfinite(total_line) or any(not math.isfinite(x) or x < 0 or x > 1 for x in total_probs):
        raise ValueError("Prediction contains invalid total probabilities or line.")
    if abs(sum(total_probs) - 1) > 0.005:
        raise ValueError("Total probabilities must sum to 100%.")
    actual_total = hs + aws
    actual_total_result = "over" if actual_total > total_line + 1e-9 else "under" if actual_total < total_line - 1e-9 else "push"
    total_actual = tuple(1.0 if actual_total_result == label else 0.0 for label in ("over", "under", "push"))
    total_brier = sum((p - y) ** 2 for p, y in zip(total_probs, total_actual))
    total_log_loss = -math.log(max(eps, total_probs[total_actual.index(1.0)]))
    spread_scores = {}
    spread_market = prediction.get("spread_market")
    if spread_market is not None:
        if not isinstance(spread_market, dict):
            raise ValueError("Prediction spread market must be an object.")
        home_spread = finite_number(
            spread_market.get("consensus_home_spread", float("nan")), "Consensus home spread"
        )
        spread_probs = tuple(
            finite_number(spread_market.get(key, -1), f"{key.title()} cover probability") / 100
            for key in ("home_cover", "away_cover", "push")
        )
        if any(not 0 <= probability <= 1 for probability in spread_probs):
            raise ValueError("Prediction contains invalid spread probabilities.")
        if abs(sum(spread_probs) - 1) > 0.005:
            raise ValueError("Spread probabilities must sum to 100%.")
        adjusted_margin = hs - aws + home_spread
        actual_spread_result = (
            "home_cover" if adjusted_margin > 1e-9 else
            "away_cover" if adjusted_margin < -1e-9 else "push"
        )
        spread_labels = ("home_cover", "away_cover", "push")
        spread_actual = tuple(1.0 if actual_spread_result == label else 0.0
                              for label in spread_labels)
        spread_brier = sum((p - y) ** 2 for p, y in zip(spread_probs, spread_actual))
        spread_log_loss = -math.log(max(eps, spread_probs[spread_labels.index(actual_spread_result)]))
        spread_scores = {
            "consensus_home_spread": home_spread,
            "actual_spread_result": actual_spread_result,
            "predicted_home_cover_probability": spread_probs[0],
            "predicted_away_cover_probability": spread_probs[1],
            "predicted_spread_push_probability": spread_probs[2],
            "spread_brier_score": spread_brier,
            "spread_log_loss": spread_log_loss,
        }
    projected = prediction.get("projected_score")
    if not isinstance(projected, dict):
        raise ValueError("Prediction projected score must be an object.")
    projected_home = finite_number(projected.get("home", float("nan")), "Projected home score")
    projected_away = finite_number(projected.get("away", float("nan")), "Projected away score")
    if not math.isfinite(projected_home) or not math.isfinite(projected_away):
        raise ValueError("Prediction contains invalid projected scores.")
    return {
        "event_id": settlement_event_id,
        "sport": str(settlement.get("sport_key") or prediction.get("sport") or ""),
        "actual_result": "home" if hs > aws else "away" if hs < aws else "tie",
        "predicted_home_probability": home,
        "predicted_away_probability": away,
        "predicted_tie_probability": tie,
        "predicted_over_probability": total_probs[0],
        "predicted_under_probability": total_probs[1],
        "predicted_total_push_probability": total_probs[2],
        "actual_total_result": actual_total_result,
        "total_brier_score": total_brier,
        "total_log_loss": total_log_loss,
        "brier_score": brier,
        "log_loss": log_loss,
        "home_score_error": abs(projected_home - hs),
        "away_score_error": abs(projected_away - aws),
        "total_score_error": abs(projected_home + projected_away - hs - aws),
        "margin_error": abs((projected_home - projected_away) - (hs - aws)),
        **spread_scores,
    }


def _mean(values):
    return sum(values) / len(values) if values else None



def _class_calibration(rows: list[dict], probability_fields: dict, actual_field: str,
                       labels: tuple[str, ...], calibration_bins: int) -> dict:
    """Build one-vs-rest reliability buckets for a mutually exclusive outcome set."""
    output = {}
    for label in labels:
        probability_key = probability_fields[label]
        eligible = [row for row in rows
                    if probability_key in row and actual_field in row]
        class_bins = []
        for index in range(calibration_bins):
            low, high = index / calibration_bins, (index + 1) / calibration_bins
            bucket = [row for row in eligible
                      if low <= row[probability_key] < high
                      or (index == calibration_bins - 1 and row[probability_key] == 1)]
            if bucket:
                predicted = _mean([row[probability_key] for row in bucket])
                observed = sum(row[actual_field] == label for row in bucket) / len(bucket)
                class_bins.append({
                    "lower_bound": round(low, 3), "upper_bound": round(high, 3),
                    "sample_size": len(bucket), "mean_predicted": predicted,
                    "observed_rate": observed,
                    "absolute_calibration_error": abs(predicted - observed),
                })
        output[label] = class_bins
    return output


def evaluate_predictions(rows: list[dict], calibration_bins: int = 10) -> dict:
    """Aggregate proper scoring rules, score errors and probability calibration."""
    if not isinstance(calibration_bins, int) or isinstance(calibration_bins, bool) or not 2 <= calibration_bins <= 20:
        raise ValueError("calibration_bins must be an integer from 2 through 20.")
    if not rows:
        return {"sample_size": 0, "status": "INSUFFICIENT_DATA", "by_sport": {},
                "calibration": [], "calibration_by_sport": {},
                "calibration_by_class": {}, "total_calibration_by_class": {},
                "spread_calibration_by_class": {}}
    by_sport = defaultdict(list)
    for row in rows:
        by_sport[row.get("sport") or "unknown"].append(row)

    def summarize(items):
        return {
            "sample_size": len(items),
            "status": "DESCRIPTIVE_ONLY" if len(items) < 200 else "EVALUATION_AVAILABLE",
            "brier_score": _mean([x["brier_score"] for x in items]),
            "log_loss": _mean([x["log_loss"] for x in items]),
            "total_brier_score": _mean([x["total_brier_score"] for x in items]),
            "total_log_loss": _mean([x["total_log_loss"] for x in items]),
            "home_score_mae": _mean([x["home_score_error"] for x in items]),
            "away_score_mae": _mean([x["away_score_error"] for x in items]),
            "total_score_mae": _mean([x["total_score_error"] for x in items]),
            "margin_mae": _mean([x["margin_error"] for x in items]),
            "spread_sample_size": sum("spread_brier_score" in x for x in items),
            "spread_brier_score": _mean([x["spread_brier_score"] for x in items if "spread_brier_score" in x]),
            "spread_log_loss": _mean([x["spread_log_loss"] for x in items if "spread_log_loss" in x]),
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
    calibration_by_class = _class_calibration(
        rows,
        {"home": "predicted_home_probability", "away": "predicted_away_probability",
         "tie": "predicted_tie_probability"},
        "actual_result", ("home", "away", "tie"), calibration_bins,
    )
    total_calibration_by_class = _class_calibration(
        rows,
        {"over": "predicted_over_probability", "under": "predicted_under_probability",
         "push": "predicted_total_push_probability"},
        "actual_total_result", ("over", "under", "push"), calibration_bins,
    )
    spread_rows = [row for row in rows if all(key in row for key in (
        "predicted_home_cover_probability", "predicted_away_cover_probability",
        "predicted_spread_push_probability", "actual_spread_result",
    ))]
    spread_calibration_by_class = _class_calibration(
        spread_rows,
        {"home_cover": "predicted_home_cover_probability",
         "away_cover": "predicted_away_cover_probability",
         "push": "predicted_spread_push_probability"},
        "actual_spread_result", ("home_cover", "away_cover", "push"), calibration_bins,
    )
    calibration_by_sport = {}
    for sport, sport_rows in sorted(by_sport.items()):
        sport_spread_rows = [row for row in sport_rows if all(key in row for key in (
            "predicted_home_cover_probability", "predicted_away_cover_probability",
            "predicted_spread_push_probability", "actual_spread_result",
        ))]
        calibration_by_sport[sport] = {
            "win": _class_calibration(
                sport_rows,
                {"home": "predicted_home_probability", "away": "predicted_away_probability",
                 "tie": "predicted_tie_probability"},
                "actual_result", ("home", "away", "tie"), calibration_bins,
            ),
            "total": _class_calibration(
                sport_rows,
                {"over": "predicted_over_probability", "under": "predicted_under_probability",
                 "push": "predicted_total_push_probability"},
                "actual_total_result", ("over", "under", "push"), calibration_bins,
            ),
            "spread": _class_calibration(
                sport_spread_rows,
                {"home_cover": "predicted_home_cover_probability",
                 "away_cover": "predicted_away_cover_probability",
                 "push": "predicted_spread_push_probability"},
                "actual_spread_result", ("home_cover", "away_cover", "push"), calibration_bins,
            ),
        }
    return {
        "sample_size": len(rows),
        "status": (
            "EVALUATION_AVAILABLE"
            if by_sport and all(len(items) >= 200 for items in by_sport.values())
            else "DESCRIPTIVE_ONLY"
        ),
        "overall": summarize(rows),
        "by_sport": {sport: summarize(items) for sport, items in sorted(by_sport.items())},
        "calibration": calibration,
        "calibration_by_class": calibration_by_class,
        "total_calibration_by_class": total_calibration_by_class,
        "spread_calibration_by_class": spread_calibration_by_class,
        "calibration_by_sport": calibration_by_sport,
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
        raise ValueError(f"Timestamp must include an explicit timezone: {value!r}")
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
    latest_time, latest_row = max(eligible, key=lambda item: (item[0], item[1]["snapshot_id"]))
    latest_snapshot_id = latest_row["snapshot_id"]
    latest_rows = [row for observed, row in eligible
                   if observed == latest_time and row["snapshot_id"] == latest_snapshot_id]
    first = latest_rows[0]
    try:
        archived_start = _parse_timestamp(first["commence_time"])
    except ValueError:
        return None, "archived event start timestamp is invalid"
    if abs((archived_start - commence).total_seconds()) > 300:
        return None, "settlement start time does not match archived event"
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



def _moneyline_market_probabilities(event: dict) -> dict | None:
    """Build median no-vig h2h probabilities from complete archived book markets."""
    from .math import no_vig_probabilities
    from .simulation import _team_identity, _team_side

    home = _team_identity(event.get("home_team"))
    away = _team_identity(event.get("away_team"))
    if not home or not away or home == away:
        return None
    grouped = {2: [], 3: []}
    seen_books = set()
    books = event.get("bookmakers", [])
    if not isinstance(books, list):
        return None
    for book in books:
        if not isinstance(book, dict):
            continue
        book_key = str(book.get("key") or book.get("title") or "").strip().lower()
        if not book_key or book_key in seen_books:
            continue
        seen_books.add(book_key)
        markets = book.get("markets", [])
        if not isinstance(markets, list):
            continue
        market = next((item for item in markets if isinstance(item, dict)
                       and str(item.get("key") or "").lower() == "h2h"), None)
        outcomes = market.get("outcomes", []) if isinstance(market, dict) else []
        if not isinstance(outcomes, list) or len(outcomes) not in (2, 3):
            continue
        sides = {}
        valid = True
        for outcome in outcomes:
            if not isinstance(outcome, dict):
                valid = False
                break
            name = str(outcome.get("name") or "").strip()
            normalized = _normalized_team(name)
            side = _team_side(name, home, away)
            if side is None and normalized in {"draw", "tie", "x"}:
                side = "draw"
            if side is None or side in sides:
                valid = False
                break
            sides[side] = outcome.get("price")
        expected_sides = {"home", "away"} if len(outcomes) == 2 else {"home", "away", "draw"}
        if not valid or set(sides) != expected_sides:
            continue
        try:
            ordered_sides = ("home", "away") if len(outcomes) == 2 else ("home", "away", "draw")
            fair = no_vig_probabilities([sides[key] for key in ordered_sides])
        except (TypeError, ValueError, OverflowError):
            continue
        if len(fair) != len(expected_sides):
            continue
        grouped[len(outcomes)].append({
            "home": fair[0], "away": fair[1],
            **({"draw": fair[2]} if len(outcomes) == 3 else {}),
        })
    if not grouped[2] and not grouped[3]:
        return None
    outcome_count = 3 if len(grouped[3]) > len(grouped[2]) else 2
    selected = grouped[outcome_count]
    labels = ("home", "away", "draw") if outcome_count == 3 else ("home", "away")
    probabilities = {label: median([row[label] for row in selected]) for label in labels}
    denominator = sum(probabilities.values())
    if denominator <= 0:
        return None
    probabilities = {label: value / denominator for label, value in probabilities.items()}
    if outcome_count == 2:
        probabilities["draw"] = 0.0
    return {"probabilities": probabilities, "outcome_count": outcome_count,
            "book_count": len(selected)}


def _score_moneyline_probabilities(probabilities: dict, outcome_count: int, settlement: dict) -> dict | None:
    """Score a no-vig moneyline market; two-way markets exclude tied finals."""
    home_score, away_score = int(settlement["home_score"]), int(settlement["away_score"])
    actual = "home" if home_score > away_score else "away" if home_score < away_score else "draw"
    if outcome_count == 2:
        if actual == "draw":
            return None
        denominator = probabilities["home"] + probabilities["away"]
        if denominator <= 0:
            return None
        probs = (probabilities["home"] / denominator, probabilities["away"] / denominator)
        actual_index = 0 if actual == "home" else 1
    else:
        probs = tuple(probabilities[key] for key in ("home", "away", "draw"))
        actual_index = {"home": 0, "away": 1, "draw": 2}[actual]
    brier = sum((probability - (1.0 if index == actual_index else 0.0)) ** 2
                for index, probability in enumerate(probs))
    return {
        "event_id": str(settlement["event_id"]),
        "sport": str(settlement.get("sport_key") or ""),
        "actual_result": actual,
        "outcome_count": outcome_count,
        "brier_score": brier,
        "log_loss": -math.log(max(1e-12, probs[actual_index])),
    }


def _moneyline_benchmark_report(model_rows: list[dict], market_rows: list[dict]) -> dict:
    """Compare simulator win probabilities with paired archived no-vig h2h consensus."""
    model_by_id = {str(row["event_id"]): row for row in model_rows}
    market_by_id = {str(row["event_id"]): row for row in market_rows}
    paired = []
    for event_id in sorted(model_by_id.keys() & market_by_id.keys()):
        model, market = model_by_id[event_id], market_by_id[event_id]
        if model.get("actual_result") != market.get("actual_result"):
            continue
        if market["outcome_count"] == 2:
            if market["actual_result"] == "draw":
                continue
            denominator = model["predicted_home_probability"] + model["predicted_away_probability"]
            if denominator <= 0:
                continue
            probs = (model["predicted_home_probability"] / denominator,
                     model["predicted_away_probability"] / denominator)
            actual_index = 0 if market["actual_result"] == "home" else 1
        else:
            probs = (model["predicted_home_probability"], model["predicted_away_probability"],
                     model["predicted_tie_probability"])
            actual_index = {"home": 0, "away": 1, "draw": 2}[market["actual_result"]]
        model_brier = sum((p - (1.0 if index == actual_index else 0.0)) ** 2
                          for index, p in enumerate(probs))
        model_log_loss = -math.log(max(1e-12, probs[actual_index]))
        paired.append({
            "event_id": event_id, "sport": market.get("sport") or "unknown",
            "model_brier": model_brier, "market_brier": market["brier_score"],
            "model_log_loss": model_log_loss, "market_log_loss": market["log_loss"],
            "market_books": market.get("book_count", 0),
        })

    def summarize(rows, seed=20261123):
        if not rows:
            return {"sample_size": 0, "simulator": None,
                    "sportsbook_no_vig_consensus": None, "simulator_minus_market": {}}
        deltas = {
            "brier_score": [row["model_brier"] - row["market_brier"] for row in rows],
            "log_loss": [row["model_log_loss"] - row["market_log_loss"] for row in rows],
        }
        rng = random.Random(seed)
        comparison = {}
        for metric, values in deltas.items():
            observed = sum(values) / len(values)
            if len(values) < 2:
                interval = probability = None
            else:
                estimates = sorted(
                    sum(values[rng.randrange(len(values))] for _ in range(len(values))) / len(values)
                    for _ in range(2000)
                )
                interval = [estimates[49], estimates[1949]]
                probability = sum(value < 0 for value in estimates) / len(estimates)
            comparison[metric] = {
                "mean_delta": observed, "ci_95": interval,
                "bootstrap_probability_delta_below_zero": probability,
                "interpretation": (
                    "INCONCLUSIVE_SINGLE_EVENT" if len(values) < 2 else
                    "DIRECTIONALLY_FAVORABLE_ON_THIS_SAMPLE" if interval[1] < 0 else
                    "DIRECTIONALLY_UNFAVORABLE_ON_THIS_SAMPLE" if interval[0] > 0 else
                    "UNCERTAINTY_INTERVAL_INCLUDES_ZERO"
                ),
            }
        return {
            "sample_size": len(rows),
            "market_book_count_mean": sum(row["market_books"] for row in rows) / len(rows),
            "simulator": {
                "brier_score": sum(row["model_brier"] for row in rows) / len(rows),
                "log_loss": sum(row["model_log_loss"] for row in rows) / len(rows),
            },
            "sportsbook_no_vig_consensus": {
                "brier_score": sum(row["market_brier"] for row in rows) / len(rows),
                "log_loss": sum(row["market_log_loss"] for row in rows) / len(rows),
            },
            "simulator_minus_market": comparison,
        }

    grouped = defaultdict(list)
    for row in paired:
        grouped[row["sport"]].append(row)
    return {
        **summarize(paired),
        "by_sport": {sport: summarize(rows, 20261123 + index + 1)
                     for index, (sport, rows) in enumerate(sorted(grouped.items()))},
        "benchmark": "median bookmaker-level no-vig h2h probabilities from the same pregame snapshot",
        "settlement_note": (
            "Two-way moneyline rows exclude tied final results because a two-way quote "
            "does not identify a draw probability; three-way markets score home/away/draw."
        ),
        "caution": "This is an archived sportsbook benchmark, not an independent forecast or proof of profitability.",
    }


def paired_bootstrap_comparison(model_rows: list[dict], baseline_rows: list[dict], iterations: int = 2000, seed: int = 20261119) -> dict:
    """Estimate uncertainty in paired model-minus-baseline metric differences.

    Resampling is by event, preserving the pairing between each fitted forecast
    and its broad-prior forecast. Negative deltas favor the fitted model.
    """
    if not isinstance(iterations, int) or isinstance(iterations, bool) or iterations < 100:
        raise ValueError("Bootstrap iterations must be an integer of at least 100.")
    model_by_id = {str(row["event_id"]): row for row in model_rows}
    baseline_by_id = {str(row["event_id"]): row for row in baseline_rows}
    if len(model_by_id) != len(model_rows) or len(baseline_by_id) != len(baseline_rows):
        raise ValueError("Paired bootstrap rows must have unique event IDs.")
    if model_by_id.keys() != baseline_by_id.keys():
        raise ValueError("Model and baseline rows must contain identical event IDs.")
    metrics = {
        "brier_score": "brier_score", "log_loss": "log_loss",
        "total_brier_score": "total_brier_score", "total_log_loss": "total_log_loss",
        "home_score_mae": "home_score_error", "away_score_mae": "away_score_error",
        "total_score_mae": "total_score_error", "margin_mae": "margin_error",
    }
    if all("spread_brier_score" in row and "spread_log_loss" in row
           for row in model_rows + baseline_rows):
        metrics["spread_brier_score"] = "spread_brier_score"
        metrics["spread_log_loss"] = "spread_log_loss"
    event_deltas = {
        label: [float(model_by_id[event_id][field]) - float(baseline_by_id[event_id][field])
                for event_id in sorted(model_by_id)]
        for label, field in metrics.items()
    }
    sample_size = len(model_rows)
    if not sample_size:
        return {"sample_size": 0, "method": "paired_event_bootstrap",
                "iterations": iterations, "metrics": {}}
    rng = random.Random(seed)
    output = {}
    for metric, deltas in event_deltas.items():
        observed = sum(deltas) / sample_size
        if sample_size < 2:
            output[metric] = {
                "mean_delta": observed, "ci_95": None,
                "bootstrap_probability_delta_below_zero": None,
                "interpretation": "INCONCLUSIVE_SINGLE_EVENT",
            }
            continue
        estimates = []
        favorable = 0
        for _ in range(iterations):
            estimate = sum(deltas[rng.randrange(sample_size)] for _ in range(sample_size)) / sample_size
            estimates.append(estimate)
            favorable += estimate < 0
        estimates.sort()
        low = estimates[int(0.025 * (iterations - 1))]
        high = estimates[int(0.975 * (iterations - 1))]
        output[metric] = {
            "mean_delta": observed,
            "ci_95": [low, high],
            "bootstrap_probability_delta_below_zero": favorable / iterations,
            "interpretation": (
                "DIRECTIONALLY_FAVORABLE_ON_THIS_SAMPLE" if high < 0 else
                "DIRECTIONALLY_UNFAVORABLE_ON_THIS_SAMPLE" if low > 0 else
                "UNCERTAINTY_INTERVAL_INCLUDES_ZERO"
            ),
        }
    grouped_model = defaultdict(list)
    grouped_baseline = defaultdict(list)
    for row in model_rows:
        grouped_model[row.get("sport") or "unknown"].append(row)
    for row in baseline_rows:
        grouped_baseline[row.get("sport") or "unknown"].append(row)
    per_sport = {
        sport: paired_bootstrap_comparison(
            grouped_model[sport], grouped_baseline[sport], iterations=iterations,
            seed=seed + index + 1,
        )
        for index, sport in enumerate(sorted(grouped_model))
    } if len(grouped_model) > 1 else {}
    return {
        "sample_size": sample_size,
        "per_sport": per_sport,
        "method": "paired_event_bootstrap",
        "iterations": iterations,
        "confidence_level": 0.95,
        "negative_delta_is_better": True,
        "metrics": output,
        "caution": "Intervals quantify sampling uncertainty on this archived sample; they do not correct selection bias, archive coverage gaps, model misspecification, or prove future performance.",
    }



def score_outcome_prediction(prediction: dict, settlement: dict) -> dict:
    """Score an outcome-only tennis/MMA forecast without inventing score markets."""
    if str(prediction.get("event_id")) != str(settlement.get("event_id")):
        raise ValueError("Prediction and settlement event IDs do not match.")
    if str(prediction.get("sport") or "") != str(settlement.get("sport_key") or ""):
        raise ValueError("Prediction and settlement leagues do not match.")
    probs = prediction.get("win_probabilities")
    if not isinstance(probs, dict):
        raise ValueError("Outcome-only prediction probabilities are missing.")
    values = [float(probs.get(key, -1)) / 100 for key in ("home", "away", "tie")]
    if any(not math.isfinite(x) or x < 0 or x > 1 for x in values) or abs(sum(values) - 1) > 0.005:
        raise ValueError("Outcome-only prediction contains invalid probabilities.")
    hs, aws = settlement.get("home_score"), settlement.get("away_score")
    if (isinstance(hs, bool) or isinstance(aws, bool)
            or not isinstance(hs, int) or not isinstance(aws, int)
            or hs < 0 or aws < 0):
        raise ValueError("Outcome-only settlement requires verified non-negative integer scores.")
    actual = "home" if hs > aws else "away" if hs < aws else "tie"
    actual_vector = (1.0, 0.0, 0.0) if actual == "home" else (0.0, 1.0, 0.0) if actual == "away" else (0.0, 0.0, 1.0)
    brier = sum((p - y) ** 2 for p, y in zip(values, actual_vector))
    index = {"home": 0, "away": 1, "tie": 2}[actual]
    return {
        "event_id": str(settlement["event_id"]),
        "sport": str(settlement["sport_key"]),
        "actual_result": actual,
        "predicted_home_probability": values[0],
        "predicted_away_probability": values[1],
        "predicted_tie_probability": values[2],
        "brier_score": brier,
        "log_loss": -math.log(max(1e-12, values[index])),
    }


def evaluate_outcome_predictions(rows: list[dict]) -> dict:
    """Summarize proper scoring rules for outcome-only leagues."""
    by_sport = defaultdict(list)
    for row in rows:
        by_sport[row.get("sport") or "unknown"].append(row)
    def mean(values):
        return sum(values) / len(values) if values else None
    def summary(items):
        return {
            "sample_size": len(items),
            "status": "DESCRIPTIVE_ONLY" if len(items) < 200 else "EVALUATION_AVAILABLE",
            "brier_score": mean([row["brier_score"] for row in items]),
            "log_loss": mean([row["log_loss"] for row in items]),
        }
    return {
        "sample_size": len(rows),
        "status": "INSUFFICIENT_DATA" if not rows else ("DESCRIPTIVE_ONLY" if len(rows) < 200 else "EVALUATION_AVAILABLE"),
        **summary(rows),
        "by_sport": {sport: summary(items) for sport, items in sorted(by_sport.items())},
    }

def run_historical_backtest(database, settlements: list[dict], seed_salt: str = "ooi-sim-v1", model_parameters: dict | None = None, include_prediction_rows: bool = False) -> dict:
    """Replay the latest real pregame snapshot per settled event; never fetch odds."""
    import hashlib
    from .simulation import run_game_simulation

    from .simulation_market_benchmark import score_archived_spread_total, _paired_report

    scored, baseline_scored, skipped, market_scored = [], [], [], []
    outcome_scored, outcome_baseline_scored = [], []
    spread_model_rows, spread_market_rows = [], []
    total_model_rows, total_market_rows = [], []
    for settlement in settlements:
        event, reason = _load_latest_pregame_event(database, settlement)
        if reason:
            skipped.append({"event_id": settlement.get("event_id"), "reason": reason})
            continue
        seed_material = f"{seed_salt}:{settlement['event_id']}".encode("utf-8")
        seed = int.from_bytes(hashlib.sha256(seed_material).digest()[:4], "big")
        try:
            market = _moneyline_market_probabilities(event)
            if market is not None:
                market_row = _score_moneyline_probabilities(
                    market["probabilities"], market["outcome_count"], settlement
                )
                if market_row is not None:
                    market_row["book_count"] = market["book_count"]
                    market_scored.append(market_row)
            baseline_prediction = run_game_simulation(event, seed=seed)
            prediction = run_game_simulation(
                event, seed=seed, model_parameters=(model_parameters or {}).get(event["sport_key"])
            )
            if prediction.get("simulation_type") == "independent_outcome_monte_carlo":
                outcome_baseline_scored.append(score_outcome_prediction(baseline_prediction, settlement))
                outcome_scored.append(score_outcome_prediction(prediction, settlement))
                continue
            baseline_row = score_prediction(baseline_prediction, settlement)
            model_row = score_prediction(prediction, settlement)
            market_lines = score_archived_spread_total(event, settlement, prediction)
            for market_type, model_rows, market_rows in (
                ("spread", spread_model_rows, spread_market_rows),
                ("total", total_model_rows, total_market_rows),
            ):
                market_line = market_lines[market_type]
                if market_line is None:
                    continue
                if market_type == "spread":
                    actual_result = model_row.get("actual_spread_result")
                    first_probability = model_row.get("predicted_home_cover_probability", 0.0)
                    second_probability = model_row.get("predicted_away_cover_probability", 0.0)
                else:
                    actual_result = model_row.get("actual_total_result")
                    first_probability = model_row.get("predicted_over_probability", 0.0)
                    second_probability = model_row.get("predicted_under_probability", 0.0)
                actual = ("first" if actual_result in ("home_cover", "over") else
                          "second" if actual_result in ("away_cover", "under") else "push")
                if actual == "push" or actual != market_line["actual"]:
                    continue
                denominator = first_probability + second_probability
                if denominator <= 0:
                    continue
                probabilities = (first_probability / denominator, second_probability / denominator)
                actual_index = 0 if actual == "first" else 1
                model_rows.append({
                    "event_id": str(settlement["event_id"]), "sport": settlement["sport_key"],
                    "brier_score": sum((p - (1.0 if i == actual_index else 0.0)) ** 2
                                       for i, p in enumerate(probabilities)),
                    "log_loss": -math.log(max(1e-12, probabilities[actual_index])),
                })
                market_rows.append({
                    "event_id": str(settlement["event_id"]), "sport": settlement["sport_key"],
                    "brier_score": market_line["brier_score"], "log_loss": market_line["log_loss"],
                    "book_count": market_line["book_count"],
                })
            baseline_scored.append(baseline_row)
            scored.append(model_row)
        except (ValueError, TypeError, KeyError) as exc:
            skipped.append({"event_id": settlement.get("event_id"), "reason": str(exc)})
    report = evaluate_predictions(scored)
    baseline_report = evaluate_predictions(baseline_scored)
    report["market_prior_baseline"] = baseline_report
    report["sportsbook_moneyline_benchmark"] = _moneyline_benchmark_report(scored, market_scored)
    report["sportsbook_spread_benchmark"] = _paired_report(
        spread_model_rows, spread_market_rows, "sportsbook_no_vig_consensus"
    )
    report["sportsbook_total_benchmark"] = _paired_report(
        total_model_rows, total_market_rows, "sportsbook_no_vig_consensus"
    )
    if scored and baseline_scored:
        report["model_minus_baseline"] = {
            key: (report["overall"][key] - baseline_report["overall"][key])
            for key in ("brier_score", "log_loss", "total_brier_score", "total_log_loss", "home_score_mae", "away_score_mae", "total_score_mae", "margin_mae", "spread_brier_score", "spread_log_loss")
            if report["overall"].get(key) is not None and baseline_report["overall"].get(key) is not None
        }
        report["paired_uncertainty"] = paired_bootstrap_comparison(scored, baseline_scored)
    if outcome_scored:
        report["outcome_only_validation"] = evaluate_outcome_predictions(outcome_scored)
        report["outcome_only_market_prior_baseline"] = evaluate_outcome_predictions(outcome_baseline_scored)
        if outcome_scored and outcome_baseline_scored:
            report["outcome_only_model_minus_market_prior"] = {
                key: report["outcome_only_validation"][key] - report["outcome_only_market_prior_baseline"][key]
                for key in ("brier_score", "log_loss")
                if report["outcome_only_validation"].get(key) is not None
                and report["outcome_only_market_prior_baseline"].get(key) is not None
            }
    report["settlements_submitted"] = len(settlements)
    report["events_scored"] = len(scored) + len(outcome_scored)
    report["outcome_only_events_scored"] = len(outcome_scored)
    report["events_skipped"] = len(skipped)
    report["skipped"] = skipped
    if include_prediction_rows:
        report["scored_predictions"] = scored
        report["scored_outcome_predictions"] = outcome_scored
    report["data_policy"] = (
        "Uses only the latest archived sportsbook snapshot strictly before each event's "
        "commence_time. No provider requests, current odds, post-start quotes, or synthetic "
        "historical prices are used. Events without a valid archived spread and total are skipped."
    )
    return report


def fit_league_models(database, training_settlements: list[dict], min_sample_size: int = 50) -> dict:
    """Fit per-league residual variance from archived pregame lines and final scores.

    This is a training-only operation. The caller must evaluate the returned
    parameters on a later, untouched chronological test set.
    """
    from .simulation import LEAGUE_PRIORS, _collect_market_inputs, _family
    from .team_model import fit_team_strength_models, fit_independent_outcome_models

    # Both model families train only from final scores; no archived odds required.
    independent_fit = fit_team_strength_models(training_settlements)
    outcome_fit = fit_independent_outcome_models(training_settlements)

    if not isinstance(min_sample_size, int) or isinstance(min_sample_size, bool) or min_sample_size < 2:
        raise ValueError("min_sample_size must be an integer of at least 2.")
    buckets = {}
    skipped = []
    for settlement in training_settlements:
        event, reason = _load_latest_pregame_event(database, settlement)
        if reason:
            skipped.append({"event_id": settlement.get("event_id"), "reason": reason})
            continue
        inputs = _collect_market_inputs(event)
        if not inputs["spread_margins"] or not inputs["totals"]:
            skipped.append({"event_id": settlement.get("event_id"), "reason": "pregame spread/total incomplete"})
            continue
        sport_key = str(settlement["sport_key"])
        family = _family(sport_key)
        if family not in LEAGUE_PRIORS:
            skipped.append({"event_id": settlement.get("event_id"), "reason": "unsupported sport family"})
            continue
        market_margin = median(inputs["spread_margins"])
        market_total = median(inputs["totals"])
        home_mean = max(0.05, (market_total + market_margin) / 2)
        away_mean = max(0.05, (market_total - market_margin) / 2)
        actual_home, actual_away = int(settlement["home_score"]), int(settlement["away_score"])
        bucket = buckets.setdefault(sport_key, {"family": family, "margin_residuals": [],
            "total_residuals": [], "home_dispersion": [], "away_dispersion": [],
            "score_residual_pairs": []})
        bucket["margin_residuals"].append((actual_home - actual_away) - market_margin)
        bucket["total_residuals"].append((actual_home + actual_away) - market_total)
        home_residual = actual_home - home_mean
        away_residual = actual_away - away_mean
        bucket["home_dispersion"].append((home_residual, home_mean))
        bucket["away_dispersion"].append((away_residual, away_mean))
        bucket["score_residual_pairs"].append(
            (home_residual, away_residual, home_mean, away_mean)
        )

    models, insufficient = {}, {}
    for sport_key, bucket in sorted(buckets.items()):
        n = len(bucket["margin_residuals"])
        if n < min_sample_size:
            insufficient[sport_key] = {"sample_size": n, "required": min_sample_size}
            continue
        family = bucket["family"]
        margin_residuals = bucket["margin_residuals"]
        total_residuals = bucket["total_residuals"]
        margin_bias = sum(margin_residuals) / n
        total_bias = sum(total_residuals) / n
        prior = LEAGUE_PRIORS[family]
        shrinkage_events = 100
        margin_variance = (sum((value - margin_bias) ** 2 for value in margin_residuals)
                           + prior["margin_sd"] ** 2 * shrinkage_events) / (n + shrinkage_events)
        total_variance = (sum((value - total_bias) ** 2 for value in total_residuals)
                          + prior["total_sd"] ** 2 * shrinkage_events) / (n + shrinkage_events)
        margin_sd = max(0.1, math.sqrt(margin_variance))
        total_sd = max(0.1, math.sqrt(total_variance))
        raw_margin_sd = math.sqrt(sum((value - margin_bias) ** 2 for value in margin_residuals) / n)
        raw_total_sd = math.sqrt(sum((value - total_bias) ** 2 for value in total_residuals) / n)
        covariance = sum((m - margin_bias) * (t - total_bias)
                         for m, t in zip(margin_residuals, total_residuals)) / n
        correlation = max(-0.95, min(0.95, covariance / max(1e-9, raw_margin_sd * raw_total_sd)))
        model = {"sport_key": sport_key, "family": family, "sample_size": n,
                 "margin_bias": margin_bias, "total_bias": total_bias,
                 "margin_sd": margin_sd, "total_sd": total_sd,
                 "margin_total_corr": correlation, "shrinkage_prior_events": shrinkage_events}
        if LEAGUE_PRIORS[family]["distribution"] == "poisson":
            for side in ("home", "away"):
                observations = bucket[side + "_dispersion"]
                numerator = sum(residual ** 2 - mean for residual, mean in observations)
                denominator = sum(mean ** 2 for _, mean in observations)
                raw_alpha = numerator / denominator if denominator else 0.0
                model[side + "_alpha"] = max(0.0, min(2.0, raw_alpha * n / (n + shrinkage_events)))
            # A shared Gamma rate factor represents positive covariance between
            # team scores; independent alpha terms retain team-specific excess variance.
            home_bias = (total_bias + margin_bias) / 2
            away_bias = (total_bias - margin_bias) / 2
            pairs = bucket["score_residual_pairs"]
            covariance_numerator = sum(
                (home_residual - home_bias) * (away_residual - away_bias)
                for home_residual, away_residual, _, _ in pairs
            )
            covariance_denominator = sum(home_mean * away_mean
                                          for _, _, home_mean, away_mean in pairs)
            raw_shared_alpha = (
                covariance_numerator / covariance_denominator
                if covariance_denominator else 0.0
            )
            shrinkage = n / (n + shrinkage_events)
            shared_alpha = max(0.0, min(2.0, raw_shared_alpha * shrinkage))
            model["shared_alpha"] = shared_alpha
            for side in ("home", "away"):
                model[side + "_alpha"] = max(0.0, model[side + "_alpha"] - shared_alpha)
        models[sport_key] = model

    # Attach independent team ratings even when archived market snapshots are
    # unavailable; only the optional residual-variance fit above needs odds.
    for sport_key, team_model in independent_fit["models"].items():
        family = _family(sport_key)
        if sport_key not in models:
            models[sport_key] = {
                "sport_key": sport_key, "family": family,
                "sample_size": team_model["training_games"],
            }
        models[sport_key]["independent_team_model"] = team_model

    for sport_key, outcome_model in outcome_fit["models"].items():
        family = _family(sport_key)
        if sport_key not in models:
            models[sport_key] = {
                "sport_key": sport_key, "family": family,
                "sample_size": outcome_model["training_matches"],
            }
        models[sport_key]["independent_outcome_model"] = outcome_model

    return {"models": models, "independent_team_model_coverage": independent_fit["league_coverage"],
            "independent_outcome_model_coverage": outcome_fit["coverage"],
            "independent_training_games": independent_fit["training_games"],
            "independent_training_matches": outcome_fit["training_matches"],
            "insufficient": insufficient,
            "training_events_used": sum(len(x["margin_residuals"]) for x in buckets.values()),
            "training_events_skipped": len(skipped), "skipped": skipped,
            "minimum_sample_size": min_sample_size,
            "warning": "Fit only on an earlier training period. Do not evaluate these fitted parameters on the same games."}



def validate_chronological_split(training_settlements: list[dict], test_settlements: list[dict]) -> None:
    """Reject overlapping event IDs and any per-league train/test time leakage."""
    training_ids = {str(row.get("event_id")) for row in training_settlements}
    test_ids = {str(row.get("event_id")) for row in test_settlements}
    overlap = sorted(training_ids & test_ids)
    if overlap:
        raise ValueError("Training and test settlements overlap by event_id: " + ", ".join(overlap[:10]))
    sports = {str(row.get("sport_key") or "") for row in training_settlements} & {
        str(row.get("sport_key") or "") for row in test_settlements
    }
    for sport in sorted(sports):
        training_times = [_parse_timestamp(row["commence_time"]) for row in training_settlements
                          if str(row.get("sport_key") or "") == sport]
        test_times = [_parse_timestamp(row["commence_time"]) for row in test_settlements
                      if str(row.get("sport_key") or "") == sport]
        if training_times and test_times and max(training_times) >= min(test_times):
            raise ValueError(f"Training games for {sport} must all precede its holdout games.")


def fit_probability_temperatures(scored_predictions: list[dict], min_sample_size: int = 100) -> dict:
    """Fit per-league multiclass temperature scaling on a separate calibration set."""
    if not isinstance(min_sample_size, int) or isinstance(min_sample_size, bool) or min_sample_size < 2:
        raise ValueError("min_sample_size must be an integer of at least 2.")
    grouped = {}
    for row in scored_predictions:
        grouped.setdefault(row.get("sport") or "unknown", []).append(row)
    temperatures, insufficient = {}, {}
    for sport, rows in sorted(grouped.items()):
        if len(rows) < min_sample_size:
            insufficient[sport] = {"sample_size": len(rows), "required": min_sample_size}
            continue
        best_temperature, best_loss = 1.0, float("inf")
        for step in range(50, 301, 2):
            temperature = step / 100.0
            losses = []
            for row in rows:
                probs = [float(row["predicted_home_probability"]),
                         float(row["predicted_away_probability"]),
                         float(row["predicted_tie_probability"])]
                weights = [max(1e-12, p) ** (1.0 / temperature) for p in probs]
                denominator = sum(weights)
                index = {"home": 0, "away": 1, "tie": 2}[row["actual_result"]]
                losses.append(-math.log(max(1e-12, weights[index] / denominator)))
            loss = sum(losses) / len(losses)
            if loss < best_loss:
                best_loss, best_temperature = loss, temperature
        best_spread_temperature = None
        spread_rows = [row for row in rows if all(key in row for key in (
            "predicted_home_cover_probability", "predicted_away_cover_probability",
            "predicted_spread_push_probability", "actual_spread_result",
        ))]
        if spread_rows:
            best_spread_temperature, best_spread_loss = 1.0, float("inf")
            for step in range(50, 301, 2):
                temperature = step / 100.0
                losses = []
                for row in spread_rows:
                    probs = [float(row["predicted_home_cover_probability"]),
                             float(row["predicted_away_cover_probability"]),
                             float(row["predicted_spread_push_probability"])]
                    weights = [max(1e-12, p) ** (1.0 / temperature) for p in probs]
                    denominator = sum(weights)
                    index = {"home_cover": 0, "away_cover": 1, "push": 2}[row["actual_spread_result"]]
                    losses.append(-math.log(max(1e-12, weights[index] / denominator)))
                loss = sum(losses) / len(losses)
                if loss < best_spread_loss:
                    best_spread_loss, best_spread_temperature = loss, temperature
        best_total_temperature, best_total_loss = 1.0, float("inf")
        for step in range(50, 301, 2):
            temperature = step / 100.0
            losses = []
            for row in rows:
                probs = [float(row["predicted_over_probability"]),
                         float(row["predicted_under_probability"]),
                         float(row["predicted_total_push_probability"])]
                weights = [max(1e-12, p) ** (1.0 / temperature) for p in probs]
                denominator = sum(weights)
                index = {"over": 0, "under": 1, "push": 2}[row["actual_total_result"]]
                losses.append(-math.log(max(1e-12, weights[index] / denominator)))
            loss = sum(losses) / len(losses)
            if loss < best_total_loss:
                best_total_loss, best_total_temperature = loss, temperature
        temperatures[sport] = {
            "temperature": best_temperature, "sample_size": len(rows),
            "calibration_log_loss": best_loss,
            "total_temperature": best_total_temperature,
            "total_calibration_log_loss": best_total_loss,
            **({"spread_temperature": best_spread_temperature,
                "spread_calibration_log_loss": best_spread_loss} if spread_rows else {}),
        }
    return {"temperatures": temperatures, "insufficient": insufficient,
            "minimum_sample_size": min_sample_size,
            "warning": "Fit only on a separate chronological calibration set, after model fitting and before the final holdout."}


def split_stored_settlements(settlements: list[dict], training_cutoff: str, calibration_cutoff: str):
    """Split stored final results by exclusive ISO dates into train/calibration/holdout."""
    from datetime import date
    try:
        training_date = date.fromisoformat(training_cutoff)
        calibration_date = date.fromisoformat(calibration_cutoff)
    except (TypeError, ValueError):
        raise ValueError("Training and calibration cutoffs must be ISO dates (YYYY-MM-DD).") from None
    if training_date >= calibration_date:
        raise ValueError("Training cutoff must be earlier than calibration cutoff.")
    training, calibration, holdout = [], [], []
    for row in settlements:
        game_date = _parse_timestamp(row["commence_time"]).date()
        if game_date < training_date:
            training.append(row)
        elif game_date < calibration_date:
            calibration.append(row)
        else:
            holdout.append(row)
    return {"training": training, "calibration": calibration, "holdout": holdout,
            "cutoffs": {"training_before": training_cutoff, "calibration_before": calibration_cutoff}}
