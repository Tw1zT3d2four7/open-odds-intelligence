"""Same-snapshot archived sportsbook benchmarks for spreads and totals.

Only complete two-sided markets at the simulator's archived consensus line are
eligible. Push settlements are excluded from binary scoring because quoted
two-way prices do not identify a push probability.
"""
from __future__ import annotations

import math
import random
from collections import defaultdict
from statistics import median

from .math import no_vig_probabilities
from .simulation import _team_identity, _team_side, _finite_point, _valid_price


def _market_rows(event: dict) -> dict:
    home = _team_identity(event.get("home_team"))
    away = _team_identity(event.get("away_team"))
    if not home or not away or home == away:
        return {"spread": [], "total": []}
    output = {"spread": [], "total": []}
    seen = set()
    for index, book in enumerate(event.get("bookmakers", []) if isinstance(event.get("bookmakers"), list) else []):
        if not isinstance(book, dict):
            continue
        identity = str(book.get("key") or book.get("title") or f"book-{index}").strip().lower()
        if not identity or identity in seen:
            continue
        seen.add(identity)
        markets = book.get("markets", [])
        if not isinstance(markets, list):
            continue
        for market in markets:
            if not isinstance(market, dict):
                continue
            key = str(market.get("key") or "").lower()
            outcomes = market.get("outcomes", [])
            if not isinstance(outcomes, list):
                continue
            if key == "spreads":
                sides = {"home": [], "away": []}
                for outcome in outcomes:
                    if not isinstance(outcome, dict):
                        continue
                    side = _team_side(outcome.get("name"), home, away)
                    point = _finite_point(outcome.get("point"))
                    price = outcome.get("price")
                    if side and point is not None and _valid_price(price):
                        sides[side].append((point, price))
                if len(sides["home"]) == len(sides["away"]) == 1:
                    hp, hprice = sides["home"][0]
                    ap, aprice = sides["away"][0]
                    if abs(hp + ap) <= 0.01:
                        output["spread"].append({
                            "book": identity, "line": -hp,
                            "probabilities": no_vig_probabilities([hprice, aprice]),
                        })
            elif key == "totals":
                sides = {"over": [], "under": []}
                for outcome in outcomes:
                    if not isinstance(outcome, dict):
                        continue
                    side = str(outcome.get("name") or "").strip().lower()
                    point = _finite_point(outcome.get("point"))
                    price = outcome.get("price")
                    if side in sides and point is not None and point > 0 and _valid_price(price):
                        sides[side].append((point, price))
                if len(sides["over"]) == len(sides["under"]) == 1:
                    op, oprice = sides["over"][0]
                    up, uprice = sides["under"][0]
                    if abs(op - up) <= 0.01:
                        output["total"].append({
                            "book": identity, "line": (op + up) / 2,
                            "probabilities": no_vig_probabilities([oprice, uprice]),
                        })
    return output


def _consensus(rows: list[dict], line: float) -> dict | None:
    # Price probabilities are only pooled when the book is quoting the exact
    # same point as the simulator's consensus; different alt-lines are not mixed.
    selected = [row for row in rows if abs(row["line"] - line) <= 0.01]
    if not selected:
        return None
    first = median([row["probabilities"][0] for row in selected])
    second = median([row["probabilities"][1] for row in selected])
    total = first + second
    if total <= 0:
        return None
    return {"first": first / total, "second": second / total, "book_count": len(selected)}


def _binary_score(probabilities: tuple[float, float], actual: str) -> dict:
    index = 0 if actual == "first" else 1
    return {
        "brier_score": sum((p - (1.0 if i == index else 0.0)) ** 2
                           for i, p in enumerate(probabilities)),
        "log_loss": -math.log(max(1e-12, probabilities[index])),
    }


def score_archived_spread_total(event: dict, settlement: dict, prediction: dict) -> dict:
    rows = _market_rows(event)
    result = {"spread": None, "total": None}
    spread = prediction.get("spread_market") or {}
    if spread:
        line = float(spread["consensus_home_spread"])
        quote = _consensus(rows["spread"], line)
        adjusted = int(settlement["home_score"]) - int(settlement["away_score"]) + line
        actual = "first" if adjusted > 1e-9 else "second" if adjusted < -1e-9 else "push"
        if quote and actual != "push":
            result["spread"] = {
                **_binary_score((quote["first"], quote["second"]), actual),
                "book_count": quote["book_count"], "line": line, "actual": actual,
            }
    total_market = prediction.get("total_market") or {}
    if total_market:
        line = float(total_market["consensus_line"])
        quote = _consensus(rows["total"], line)
        actual_total = int(settlement["home_score"]) + int(settlement["away_score"])
        actual = "first" if actual_total > line + 1e-9 else "second" if actual_total < line - 1e-9 else "push"
        if quote and actual != "push":
            result["total"] = {
                **_binary_score((quote["first"], quote["second"]), actual),
                "book_count": quote["book_count"], "line": line, "actual": actual,
            }
    return result


def _paired_report(model_rows: list[dict], market_rows: list[dict], market_name: str) -> dict:
    model_by_id = {str(row["event_id"]): row for row in model_rows}
    market_by_id = {str(row["event_id"]): row for row in market_rows}
    paired = []
    for event_id in sorted(model_by_id.keys() & market_by_id.keys()):
        model, market = model_by_id[event_id], market_by_id[event_id]
        for metric in ("brier_score", "log_loss"):
            pass
        paired.append({
            "event_id": event_id, "sport": model.get("sport") or "unknown",
            "model_brier": model["brier_score"], "market_brier": market["brier_score"],
            "model_log_loss": model["log_loss"], "market_log_loss": market["log_loss"],
            "market_books": market["book_count"],
        })

    def summarize(items):
        if not items:
            return {"sample_size": 0, "simulator": None, market_name: None,
                    "simulator_minus_market": {}}
        rng = random.Random(20261124)
        comparison = {}
        for metric in ("brier_score", "log_loss"):
            deltas = [row[f"model_{metric}"] - row[f"market_{metric}"] for row in items]
            observed = sum(deltas) / len(deltas)
            if len(deltas) < 2:
                interval = probability = None
            else:
                estimates = sorted(
                    sum(deltas[rng.randrange(len(deltas))] for _ in deltas) / len(deltas)
                    for _ in range(2000)
                )
                interval = [estimates[49], estimates[1949]]
                probability = sum(value < 0 for value in estimates) / len(estimates)
            comparison[metric] = {
                "mean_delta": observed, "ci_95": interval,
                "bootstrap_probability_delta_below_zero": probability,
                "interpretation": (
                    "INCONCLUSIVE_SINGLE_EVENT" if len(deltas) < 2 else
                    "DIRECTIONALLY_FAVORABLE_ON_THIS_SAMPLE" if interval[1] < 0 else
                    "DIRECTIONALLY_UNFAVORABLE_ON_THIS_SAMPLE" if interval[0] > 0 else
                    "UNCERTAINTY_INTERVAL_INCLUDES_ZERO"
                ),
            }
        return {
            "sample_size": len(items),
            "market_book_count_mean": sum(row["market_books"] for row in items) / len(items),
            "simulator": {
                "brier_score": sum(row["model_brier"] for row in items) / len(items),
                "log_loss": sum(row["model_log_loss"] for row in items) / len(items),
            },
            market_name: {
                "brier_score": sum(row["market_brier"] for row in items) / len(items),
                "log_loss": sum(row["market_log_loss"] for row in items) / len(items),
            },
            "simulator_minus_market": comparison,
        }

    by_sport = defaultdict(list)
    for row in paired:
        by_sport[row["sport"]].append(row)
    return {
        **summarize(paired),
        "by_sport": {sport: summarize(items) for sport, items in sorted(by_sport.items())},
        "benchmark": "same-snapshot median bookmaker no-vig probabilities at the simulator consensus line",
        "settlement_note": "Pushes are excluded: two-way quoted prices do not identify a push probability.",
        "caution": "Archived market comparison is not an independent forecast or proof of profitability.",
    }
