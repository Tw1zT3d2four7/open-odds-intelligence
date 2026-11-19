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
    if not records:
        raise ValueError("Settlement CSV contains no result rows.")
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
    total_market = prediction.get("total_market") or {}
    total_line = float(total_market.get("consensus_line", float("nan")))
    total_probs = tuple(float(total_market.get(key, -1)) / 100 for key in ("over", "under", "push"))
    if not math.isfinite(total_line) or any(not math.isfinite(x) or x < 0 or x > 1 for x in total_probs):
        raise ValueError("Prediction contains invalid total probabilities or line.")
    if abs(sum(total_probs) - 1) > 0.005:
        raise ValueError("Total probabilities must sum to 100%.")
    actual_total = hs + aws
    actual_total_result = "over" if actual_total > total_line + 1e-9 else "under" if actual_total < total_line - 1e-9 else "push"
    total_actual = tuple(1.0 if actual_total_result == label else 0.0 for label in ("over", "under", "push"))
    total_brier = sum((p - y) ** 2 for p, y in zip(total_probs, total_actual))
    total_log_loss = -math.log(max(eps, total_probs[total_actual.index(1.0)]))
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
            "total_brier_score": _mean([x["total_brier_score"] for x in items]),
            "total_log_loss": _mean([x["total_log_loss"] for x in items]),
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
    calibration_by_class = {}
    for label in ("home", "away", "tie"):
        probability_key = "predicted_" + label + "_probability"
        class_bins = []
        for index in range(calibration_bins):
            low, high = index / calibration_bins, (index + 1) / calibration_bins
            bucket = [x for x in rows if low <= x[probability_key] < high
                      or (index == calibration_bins - 1 and x[probability_key] == 1)]
            if bucket:
                predicted = _mean([x[probability_key] for x in bucket])
                observed = sum(x["actual_result"] == label for x in bucket) / len(bucket)
                class_bins.append({
                    "lower_bound": round(low, 3), "upper_bound": round(high, 3),
                    "sample_size": len(bucket), "mean_predicted": predicted,
                    "observed_rate": observed, "absolute_calibration_error": abs(predicted - observed),
                })
        calibration_by_class[label] = class_bins
    total_calibration_by_class = {}
    total_probability_keys = {"over": "predicted_over_probability",
                              "under": "predicted_under_probability",
                              "push": "predicted_total_push_probability"}
    for label, probability_key in total_probability_keys.items():
        class_bins = []
        for index in range(calibration_bins):
            low, high = index / calibration_bins, (index + 1) / calibration_bins
            bucket = [x for x in rows if low <= x[probability_key] < high
                      or (index == calibration_bins - 1 and x[probability_key] == 1)]
            if bucket:
                predicted = _mean([x[probability_key] for x in bucket])
                observed = sum(x["actual_total_result"] == label for x in bucket) / len(bucket)
                class_bins.append({
                    "lower_bound": round(low, 3), "upper_bound": round(high, 3),
                    "sample_size": len(bucket), "mean_predicted": predicted,
                    "observed_rate": observed, "absolute_calibration_error": abs(predicted - observed),
                })
        total_calibration_by_class[label] = class_bins
    return {
        "sample_size": len(rows),
        "status": "DESCRIPTIVE_ONLY" if len(rows) < 200 else "EVALUATION_AVAILABLE",
        "overall": summarize(rows),
        "by_sport": {sport: summarize(items) for sport, items in sorted(by_sport.items())},
        "calibration": calibration,
        "calibration_by_class": calibration_by_class,
        "total_calibration_by_class": total_calibration_by_class,
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
    return {
        "sample_size": sample_size,
        "method": "paired_event_bootstrap",
        "iterations": iterations,
        "confidence_level": 0.95,
        "negative_delta_is_better": True,
        "metrics": output,
        "caution": "Intervals quantify sampling uncertainty on this archived sample; they do not correct selection bias, archive coverage gaps, model misspecification, or prove future performance.",
    }


def run_historical_backtest(database, settlements: list[dict], seed_salt: str = "ooi-sim-v1", model_parameters: dict | None = None, include_prediction_rows: bool = False) -> dict:
    """Replay the latest real pregame snapshot per settled event; never fetch odds."""
    import hashlib
    from .simulation import run_game_simulation

    scored, baseline_scored, skipped = [], [], []
    for settlement in settlements:
        event, reason = _load_latest_pregame_event(database, settlement)
        if reason:
            skipped.append({"event_id": settlement.get("event_id"), "reason": reason})
            continue
        seed_material = f"{seed_salt}:{settlement['event_id']}".encode("utf-8")
        seed = int.from_bytes(hashlib.sha256(seed_material).digest()[:4], "big")
        try:
            baseline_prediction = run_game_simulation(event, seed=seed)
            baseline_row = score_prediction(baseline_prediction, settlement)
            prediction = run_game_simulation(
                event, seed=seed, model_parameters=(model_parameters or {}).get(event["sport_key"])
            )
            model_row = score_prediction(prediction, settlement)
            baseline_scored.append(baseline_row)
            scored.append(model_row)
        except (ValueError, TypeError, KeyError) as exc:
            skipped.append({"event_id": settlement.get("event_id"), "reason": str(exc)})
    report = evaluate_predictions(scored)
    baseline_report = evaluate_predictions(baseline_scored)
    report["market_prior_baseline"] = baseline_report
    if scored and baseline_scored:
        report["model_minus_baseline"] = {
            key: (report["overall"][key] - baseline_report["overall"][key])
            for key in ("brier_score", "log_loss", "total_brier_score", "total_log_loss", "home_score_mae", "away_score_mae", "total_score_mae", "margin_mae")
        }
        report["paired_uncertainty"] = paired_bootstrap_comparison(scored, baseline_scored)
    report["settlements_submitted"] = len(settlements)
    report["events_scored"] = len(scored)
    report["events_skipped"] = len(skipped)
    report["skipped"] = skipped
    if include_prediction_rows:
        report["scored_predictions"] = scored
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
            "total_residuals": [], "home_dispersion": [], "away_dispersion": []})
        bucket["margin_residuals"].append((actual_home - actual_away) - market_margin)
        bucket["total_residuals"].append((actual_home + actual_away) - market_total)
        bucket["home_dispersion"].append((actual_home - home_mean, home_mean))
        bucket["away_dispersion"].append((actual_away - away_mean, away_mean))

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
        models[sport_key] = model
    return {"models": models, "insufficient": insufficient,
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
        }
    return {"temperatures": temperatures, "insufficient": insufficient,
            "minimum_sample_size": min_sample_size,
            "warning": "Fit only on a separate chronological calibration set, after model fitting and before the final holdout."}
