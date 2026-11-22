import json
import math
import random
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

TRIALS = 10_000
POINT_TOLERANCE = 0.01

# Score and margin volatility are broad league-level priors. The current
# sportsbook consensus supplies the game-specific expected margin and total.
LEAGUE_PRIORS = {
    "basketball": {"margin_sd": 12.0, "total_sd": 17.0, "distribution": "normal"},
    "football": {"margin_sd": 13.5, "total_sd": 13.0, "distribution": "normal"},
    "baseball": {"margin_sd": 3.2, "total_sd": 4.0, "distribution": "poisson"},
    "hockey": {"margin_sd": 2.4, "total_sd": 1.9, "distribution": "poisson"},
    "soccer": {"margin_sd": 1.55, "total_sd": 1.25, "distribution": "poisson"},
}
SPORT_FAMILY = {
    "basketball": "basketball", "football": "football",
    "baseball": "baseball", "hockey": "hockey", "soccer": "soccer",
    "tennis": "tennis", "mma": "mma", "mixed_martial_arts": "mma",
}


def _family(sport_key):
    key = str(sport_key or "").lower()
    for token, family in SPORT_FAMILY.items():
        if token in key:
            return family
    return None


def _decimal(price):
    if isinstance(price, bool):
        raise ValueError("Invalid American odds; expected a non-zero integer")
    try:
        price = float(price)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Invalid American odds; expected a non-zero integer") from None
    if (not math.isfinite(price) or price == 0 or not price.is_integer()
            or -100 < price < 100):
        raise ValueError("Invalid American odds; expected +100 or greater, or -100 or lower")
    return 1 + (price / 100 if price > 0 else 100 / abs(price))


def _team_identity(value):
    """Normalize provider team labels without relying on display-case equality."""
    return "".join(character.lower() for character in str(value or "") if character.isalnum())


def _team_side(value, home, away):
    """Resolve provider labels such as 'Houston Texans' to event label 'Houston'.

    Prefix matching is only accepted when it identifies exactly one event team.
    This avoids silently assigning a quote when both teams share an abbreviation.
    """
    name = _team_identity(value)
    if not name:
        return None
    if name == home:
        return "home"
    if name == away:
        return "away"
    matches = []
    for side, team in (("home", home), ("away", away)):
        if len(team) >= 4 and (name.startswith(team) or team.startswith(name)):
            matches.append(side)
    return matches[0] if len(matches) == 1 else None


def _finite_point(value):
    try:
        point = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return point if math.isfinite(point) else None


def _valid_price(value):
    try:
        _decimal(value)
        return True
    except (TypeError, ValueError, OverflowError):
        return False


def _book_key(book, index):
    if not isinstance(book, dict):
        return f"invalid-book-{index}"
    return str(book.get("key") or book.get("title") or f"book-{index}").strip().lower()


def _market_candidates(book, market_key):
    markets = book.get("markets", []) if isinstance(book, dict) else []
    if not isinstance(markets, list):
        return []
    return [
        market for market in markets
        if isinstance(market, dict)
        and str(market.get("key") or "").strip().lower() == market_key
    ]


def _spread_quote(event, book):
    home = _team_identity(event.get("home_team"))
    away = _team_identity(event.get("away_team"))
    if not home or not away or home == away:
        return None, "event home/away team identity is missing or ambiguous"
    candidates = _market_candidates(book, "spreads")
    if not candidates:
        return None, "spread market is missing"
    for market in candidates:
        outcomes = market.get("outcomes", [])
        if not isinstance(outcomes, list):
            continue
        sides = {"home": [], "away": []}
        for outcome in outcomes:
            if not isinstance(outcome, dict):
                continue
            side = _team_side(outcome.get("name"), home, away)
            point = _finite_point(outcome.get("point"))
            price = outcome.get("price")
            if side and point is not None and (price is None or _valid_price(price)):
                sides[side].append(point)
        if len(sides["home"]) != 1 or len(sides["away"]) != 1:
            continue
        home_point, away_point = sides["home"][0], sides["away"][0]
        if abs(home_point + away_point) > POINT_TOLERANCE:
            continue
        return -home_point, None
    return None, "spread must contain one complete, valid, complementary home and away quote"


def _total_quote(book):
    candidates = _market_candidates(book, "totals")
    if not candidates:
        return None, "total market is missing"
    for market in candidates:
        outcomes = market.get("outcomes", [])
        if not isinstance(outcomes, list):
            continue
        sides = {"over": [], "under": []}
        for outcome in outcomes:
            if not isinstance(outcome, dict):
                continue
            side = str(outcome.get("name") or "").strip().lower()
            point = _finite_point(outcome.get("point"))
            price = outcome.get("price")
            if side in sides and point is not None and point > 0 and (price is None or _valid_price(price)):
                sides[side].append(point)
        if len(sides["over"]) != 1 or len(sides["under"]) != 1:
            continue
        over_point, under_point = sides["over"][0], sides["under"][0]
        if abs(over_point - under_point) > POINT_TOLERANCE:
            continue
        return median((over_point, under_point)), None
    return None, "total must contain one valid, matching Over and Under quote"



def _moneyline_inputs(event):
    """Build a median no-vig two- or three-way h2h consensus."""
    home = _team_identity(event.get("home_team"))
    away = _team_identity(event.get("away_team"))
    if not home or not away or home == away:
        return [], [], []
    probability_rows, diagnostics = [], []
    seen_books = set()
    bookmakers = event.get("bookmakers", [])
    if not isinstance(bookmakers, list):
        bookmakers = []
    for index, book in enumerate(bookmakers):
        if not isinstance(book, dict):
            continue
        identity = _book_key(book, index)
        if identity in seen_books:
            diagnostics.append({"bookmaker": identity, "h2h": "duplicate bookmaker ignored"})
            continue
        seen_books.add(identity)
        usable = None
        for market in _market_candidates(book, "h2h"):
            outcomes = market.get("outcomes", [])
            if not isinstance(outcomes, list):
                continue
            sides = {"home": [], "away": [], "draw": []}
            for outcome in outcomes:
                if not isinstance(outcome, dict) or not _valid_price(outcome.get("price")):
                    continue
                label = _team_identity(outcome.get("name"))
                side = _team_side(outcome.get("name"), home, away)
                if side:
                    sides[side].append(_decimal(outcome["price"]))
                elif label in ("draw", "tie", "x"):
                    sides["draw"].append(_decimal(outcome["price"]))
            if (len(sides["home"]) == 1 and len(sides["away"]) == 1
                    and len(sides["draw"]) <= 1):
                implied = [1.0 / sides["home"][0], 1.0 / sides["away"][0]]
                if sides["draw"]:
                    implied.append(1.0 / sides["draw"][0])
                denominator = sum(implied)
                if denominator > 0:
                    normalized = [value / denominator for value in implied]
                    if len(normalized) == 2:
                        normalized.append(0.0)
                    usable = normalized
                    break
        if usable is None:
            diagnostics.append({"bookmaker": identity,
                                "h2h": "requires one valid home and away outcome and, optionally, one draw"})
            continue
        probability_rows.append((usable, identity))
    if not probability_rows:
        return [], [], diagnostics
    # Never blend two-way and three-way markets: their draw settlement differs.
    groups = {
        count: [(row, identity) for row, identity in probability_rows
                if (3 if row[2] > 0 else 2) == count]
        for count in (2, 3)
    }
    selected_count = max((count for count in (2, 3) if groups[count]),
                         key=lambda count: (len(groups[count]), count))
    selected = groups[selected_count]
    excluded = [identity for count in (2, 3) if count != selected_count
                for _, identity in groups[count]]
    if excluded:
        diagnostics.append({
            "bookmaker": ", ".join(excluded),
            "h2h": "excluded because two-way and three-way moneyline settlement conventions are not mixed",
        })
    books = [identity for _, identity in selected]
    # Median each outcome, then renormalize to preserve a valid probability vector.
    medians = [median([row[index] for row, _ in selected]) for index in range(3)]
    total = sum(medians)
    probabilities = [value / total for value in medians] if total > 0 else []
    return probabilities, books, diagnostics


def _run_outcome_only_simulation(event, seed, family):
    probabilities, books, diagnostics = _moneyline_inputs(event)
    if not probabilities:
        raise ValueError(
            "This event requires a complete two-way or three-way h2h moneyline from at least one bookmaker. "
            "A score spread and total are not required for this market-only mode."
        )
    home_probability, away_probability, draw_probability = probabilities
    rng = random.Random(seed)
    counts = [0, 0, 0]
    for _ in range(TRIALS):
        draw = rng.random()
        if draw < home_probability:
            counts[0] += 1
        elif draw < home_probability + away_probability:
            counts[1] += 1
        else:
            counts[2] += 1
    home_pct = round(counts[0] / TRIALS * 100, 2)
    away_pct = round(counts[1] / TRIALS * 100, 2)
    draw_pct = round(100.0 - home_pct - away_pct, 2)
    sport_key = str(event.get("sport_key") or event.get("sport") or "")
    kind = (
        "tennis_match" if family == "tennis" else
        "combat_contest" if family == "mma" else
        "generic_two_or_three_way_event"
    )
    return {
        "trials": TRIALS,
        "simulation_seed": seed,
        "simulation_type": "moneyline_outcome_only",
        "sport": sport_key,
        "event_id": event.get("id"),
        "matchup": f'{event.get("away_team")} vs {event.get("home_team")}',
        "commence_time": event.get("commence_time"),
        "home_team": event.get("home_team"),
        "away_team": event.get("away_team"),
        "win_probabilities": {"home": home_pct, "away": away_pct, "tie": draw_pct},
        "market_moneyline": {
            "home_probability": round(home_probability * 100, 2),
            "away_probability": round(away_probability * 100, 2),
            "draw_probability": round(draw_probability * 100, 2),
            "outcome_count": 3 if draw_probability > 0 else 2,
            "book_count": len(books),
            "bookmakers": books,
        },
        "data_quality": {
            "model_source": "sportsbook_no_vig_consensus",
            "simulation_mode": kind,
            "moneyline_books": len(books),
            "distribution": "categorical_market_consensus",
            "training_sample_size": 0,
            "market_diagnostics": diagnostics,
        },
        "methodology": (
            "10,000 outcome trials sampled from the median bookmaker-level no-vig "
            "two-way or three-way moneyline consensus. This exposes the market-implied "
            "outcome distribution; it is not an independent prediction or a score simulation."
        ),
    }


def _collect_market_inputs(event):
    spread_margins, totals = [], []
    spread_books, total_books = [], []
    diagnostics = []
    bookmakers = event.get("bookmakers", [])
    if not isinstance(bookmakers, list):
        bookmakers = []

    seen_books = set()
    for index, book in enumerate(bookmakers):
        if not isinstance(book, dict):
            diagnostics.append({"bookmaker": f"invalid-book-{index}", "spread": "invalid bookmaker record", "total": "invalid bookmaker record"})
            continue
        identity = _book_key(book, index)
        if identity in seen_books:
            diagnostics.append({"bookmaker": identity, "spread": "duplicate bookmaker ignored", "total": "duplicate bookmaker ignored"})
            continue
        seen_books.add(identity)

        spread, spread_error = _spread_quote(event, book)
        total, total_error = _total_quote(book)
        if spread_error is None:
            spread_margins.append(spread)
            spread_books.append(identity)
        if total_error is None:
            totals.append(total)
            total_books.append(identity)
        if spread_error or total_error:
            diagnostics.append({
                "bookmaker": identity,
                "spread": spread_error,
                "total": total_error,
            })

    return {
        "spread_margins": spread_margins,
        "totals": totals,
        "spread_books": spread_books,
        "total_books": total_books,
        "diagnostics": diagnostics,
    }


def _market_inputs(event):
    inputs = _collect_market_inputs(event)
    spread_margins = inputs["spread_margins"]
    totals = inputs["totals"]
    missing = []
    if not spread_margins:
        missing.append("a complete, valid spread with matching home and away outcomes")
    if not totals:
        missing.append("a complete, valid total with matching Over and Under outcomes")
    if missing:
        raise ValueError(
            "The simulator requires usable spread and total lines. The stored odds "
            "could not provide " + " and ".join(missing) +
            ". Check the market diagnostics and provider outcome/team-name mapping."
        )
    return median(spread_margins), median(totals), len(spread_margins), len(totals)


def _validate_upcoming_event(event):
    raw_start = event.get("commence_time")
    if not isinstance(raw_start, str) or not raw_start.strip():
        raise ValueError("A valid upcoming game start time is required for simulation.")
    try:
        start = datetime.fromisoformat(raw_start.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Game start time must be a valid ISO-8601 timestamp.") from None
    if start.tzinfo is None or start.utcoffset() is None:
        raise ValueError("Game start time must include an explicit timezone.")
    if start.astimezone(timezone.utc) <= datetime.now(timezone.utc):
        raise ValueError("Simulation is only available before the game starts.")
    return start


def market_readiness(event):
    """Report whether stored quotes and schedule permit a pregame simulation."""
    inputs = _collect_market_inputs(event)
    family = _family(event.get("sport_key") or event.get("sport"))
    has_start_time = True
    has_spread = bool(inputs["spread_margins"])
    has_total = bool(inputs["totals"])
    if family in ("tennis", "mma") or family is None or not (has_spread and has_total):
        moneyline, books, diagnostics = _moneyline_inputs(event)
        missing = []
        try:
            _validate_upcoming_event(event)
        except ValueError as exc:
            has_start_time = False
            missing.append(str(exc))
        if not moneyline:
            missing.append("a complete two-way or three-way h2h moneyline")
            if family not in ("tennis", "mma") and family is not None:
                if not has_spread:
                    missing.append("a complete, valid spread")
                if not has_total:
                    missing.append("a complete, valid total")
        return {
            "can_simulate": has_start_time and bool(moneyline),
            "simulation_mode": "moneyline_outcome_only",
            "has_start_time": has_start_time,
            "has_h2h": bool(moneyline),
            "has_spread": has_spread,
            "has_total": has_total,
            "moneyline_books": len(books),
            "spread_books": len(inputs["spread_books"]),
            "total_books": len(inputs["total_books"]),
            "reason": None if not missing else "Missing " + " and ".join(missing) + ".",
            "market_diagnostics": diagnostics + inputs["diagnostics"],
        }
    missing = []
    try:
        _validate_upcoming_event(event)
    except ValueError as exc:
        has_start_time = False
        missing.append(str(exc))
    if not has_spread:
        missing.append("a complete, valid spread with matching home and away outcomes")
    if not has_total:
        missing.append("a complete, valid total with matching Over and Under outcomes")
    return {
        "can_simulate": has_start_time and has_spread and has_total,
        "has_start_time": has_start_time,
        "has_spread": has_spread,
        "has_total": has_total,
        "spread_books": len(inputs["spread_books"]),
        "total_books": len(inputs["total_books"]),
        "reason": None if not missing else "Missing " + " and ".join(missing) + ".",
        "market_diagnostics": inputs["diagnostics"],
    }


def _distribution_summary(values):
    """Summarize simulated outcomes with empirical percentiles, not confidence intervals."""
    if not values:
        raise ValueError("A simulation distribution cannot be empty.")
    ordered = sorted(float(value) for value in values)
    count = len(ordered)

    def percentile(probability):
        position = (count - 1) * probability
        lower = int(math.floor(position))
        upper = int(math.ceil(position))
        if lower == upper:
            return round(ordered[lower], 2)
        fraction = position - lower
        return round(ordered[lower] * (1 - fraction) + ordered[upper] * fraction, 2)

    mean = sum(ordered) / count
    variance = sum((value - mean) ** 2 for value in ordered) / count
    return {
        "mean": round(mean, 2),
        "standard_deviation": round(math.sqrt(variance), 2),
        "p10": percentile(0.10),
        "median": percentile(0.50),
        "p90": percentile(0.90),
        "interval": "empirical_p10_p90",
    }


def _poisson(rng, mean):
    if mean <= 0:
        return 0
    limit = math.exp(-mean)
    product, count = 1.0, 0
    while product > limit:
        count += 1
        product *= rng.random()
    return count - 1


def _negative_binomial(rng, mean, alpha):
    """Gamma-Poisson mixture; alpha=0 reduces exactly to Poisson."""
    if alpha <= 1e-12:
        return _poisson(rng, mean)
    shape = 1.0 / alpha
    latent_mean = rng.gammavariate(shape, mean / shape)
    return _poisson(rng, latent_mean)


def _validated_model_parameters(parameters, family, sport_key):
    if parameters is None:
        return None
    if (not isinstance(parameters, dict) or parameters.get("family") != family
            or parameters.get("sport_key") != sport_key):
        raise ValueError("Fitted model parameters do not match the selected league.")
    result = dict(parameters)
    sample_size = result.get("sample_size", 0)
    if isinstance(sample_size, bool) or not isinstance(sample_size, int) or sample_size < 0:
        raise ValueError("Invalid fitted model parameter: sample_size.")
    bounds = {"margin_sd": (0.1, 50.0), "total_sd": (0.1, 60.0),
              "margin_total_corr": (-0.95, 0.95), "margin_bias": (-50.0, 50.0),
              "total_bias": (-100.0, 100.0), "temperature": (0.5, 3.0),
              "total_temperature": (0.5, 3.0), "spread_temperature": (0.5, 3.0),
              "home_alpha": (0.0, 2.0),
              "away_alpha": (0.0, 2.0), "shared_alpha": (0.0, 2.0)}
    for key, (low, high) in bounds.items():
        if key not in result:
            continue
        if isinstance(result[key], bool):
            raise ValueError(f"Invalid fitted model parameter: {key}.")
        try:
            value = float(result[key])
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f"Invalid fitted model parameter: {key}.") from None
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"Invalid fitted model parameter: {key}.")
        result[key] = value
    return result



def load_fitted_models(path):
    """Load operator-generated per-league model JSON; absent file means priors."""
    if not path or not Path(path).is_file():
        return {}
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read fitted simulation models: {exc}") from None
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, dict):
        raise ValueError("Fitted simulation model file must contain a models object.")
    for sport_key, parameters in models.items():
        family = _family(sport_key)
        if family is None:
            raise ValueError(f"Unsupported league in fitted model file: {sport_key}")
        _validated_model_parameters(parameters, family, sport_key)
    return models



def _resolve_tie(rng, sport_key, home_score, away_score):
    """Apply simplified league-specific overtime/extra-inning resolution."""
    if home_score != away_score:
        return home_score, away_score, False
    key = str(sport_key or "").lower()
    if "soccer" in key:
        return home_score, away_score, False
    if "icehockey_nhl" in key:
        for _ in range(20):
            home_ot, away_ot = _poisson(rng, 0.22), _poisson(rng, 0.22)
            if home_ot != away_ot:
                return home_score + home_ot, away_score + away_ot, True
        return (home_score + 1, away_score, True) if rng.random() < 0.5 else (home_score, away_score + 1, True)
    if key.startswith("basketball_"):
        period_mean = 10.5 if key == "basketball_nba" else 7.0 if key == "basketball_ncaab" else 8.0
        for _ in range(100):
            home_ot, away_ot = _poisson(rng, period_mean), _poisson(rng, period_mean)
            if home_ot != away_ot:
                return home_score + home_ot, away_score + away_ot, True
        return (home_score + 2, away_score, True) if rng.random() < 0.5 else (home_score, away_score + 2, True)
    if "baseball_mlb" in key:
        for _ in range(100):
            home_ot, away_ot = _poisson(rng, 0.65), _poisson(rng, 0.65)
            if home_ot != away_ot:
                return home_score + home_ot, away_score + away_ot, True
        return (home_score + 1, away_score, True) if rng.random() < 0.5 else (home_score, away_score + 1, True)
    if "americanfootball_ncaaf" in key:
        outcomes = ((0, 0.12), (3, 0.18), (6, 0.08), (7, 0.52), (8, 0.10))
        values = [item[0] for item in outcomes]
        weights = [item[1] for item in outcomes]
        for _ in range(100):
            home_ot = rng.choices(values, weights=weights, k=1)[0]
            away_ot = rng.choices(values, weights=weights, k=1)[0]
            if home_ot != away_ot:
                return home_score + home_ot, away_score + away_ot, True
        return (home_score + 3, away_score, True) if rng.random() < 0.5 else (home_score, away_score + 3, True)
    if "americanfootball_nfl" in key:
        # Regular-season overtime ties are rare. Independent team score draws
        # from the same 0/3/7 distribution made equal scores far too common:
        # 40.5% of overtime trials. Use a conservative historical tie-rate
        # prior (about 6% of OT games in 2012-2024); the 2025 both-teams-possession
        # rule has only a small early sample, so this remains an approximation.
        if rng.random() < 0.06:
            return home_score, away_score, True
        outcomes = ((0, 0.55), (3, 0.25), (7, 0.20))
        values = [item[0] for item in outcomes]
        weights = [item[1] for item in outcomes]
        for _ in range(100):
            home_ot = rng.choices(values, weights=weights, k=1)[0]
            away_ot = rng.choices(values, weights=weights, k=1)[0]
            if home_ot != away_ot:
                return home_score + home_ot, away_score + away_ot, True
        return (home_score + 3, away_score, True) if rng.random() < 0.5 else (home_score, away_score + 3, True)
    return home_score, away_score, False


def _draw_regulation_scores(rng, family, expected_margin, expected_total, prior, fitted):
    """Draw regulation scores from market anchors before overtime is applied."""
    if prior["distribution"] == "poisson":
        total_bias = fitted.get("total_bias", 0.0) if fitted else 0.0
        margin_bias = fitted.get("margin_bias", 0.0) if fitted else 0.0
        home_mean = max(0.05, (expected_total + total_bias + expected_margin + margin_bias) / 2)
        away_mean = max(0.05, (expected_total + total_bias - expected_margin - margin_bias) / 2)
        home_alpha = fitted.get("home_alpha", 0.0) if fitted else 0.0
        away_alpha = fitted.get("away_alpha", 0.0) if fitted else 0.0
        shared_alpha = fitted.get("shared_alpha", 0.0) if fitted else 0.0
        shared_multiplier = (
            rng.gammavariate(1.0 / shared_alpha, shared_alpha)
            if shared_alpha > 1e-12 else 1.0
        )
        home_score = _negative_binomial(rng, home_mean * shared_multiplier, home_alpha)
        away_score = _negative_binomial(rng, away_mean * shared_multiplier, away_alpha)
        return home_score, away_score

    z_margin = rng.gauss(0.0, 1.0)
    z_independent = rng.gauss(0.0, 1.0)
    correlation = fitted.get("margin_total_corr", 0.0) if fitted else 0.0
    margin = expected_margin + (fitted.get("margin_bias", 0.0) if fitted else 0.0) + z_margin * prior["margin_sd"]
    total = expected_total + (fitted.get("total_bias", 0.0) if fitted else 0.0) + prior["total_sd"] * (
        correlation * z_margin + math.sqrt(1.0 - correlation ** 2) * z_independent
    )
    total = max(0.0, total)
    return max(0, round((total + margin) / 2)), max(0, round((total - margin) / 2))


def _estimate_overtime_total_adjustment(sport_key, family, expected_margin, expected_total, prior, fitted, seed):
    """Estimate points added after regulation so full-game market totals stay anchored.

    Sportsbook totals generally include overtime/extra innings. The simulator
    must therefore lower its regulation scoring anchor by the expected points
    added when a regulation tie occurs, rather than adding overtime on top of
    an already full-game total.
    """
    if "soccer" in str(sport_key or "").lower():
        return 0.0
    adjustment = 0.0
    pilot_seed = (seed ^ 0x5A17C0DE) if seed is not None else None
    for _ in range(2):
        pilot_rng = random.Random(pilot_seed)
        added_points = 0
        for _ in range(TRIALS):
            home, away = _draw_regulation_scores(
                pilot_rng, family, expected_margin, expected_total - adjustment, prior, fitted
            )
            if home == away:
                final_home, final_away, _ = _resolve_tie(pilot_rng, sport_key, home, away)
                added_points += final_home + final_away - home - away
        revised = added_points / TRIALS
        if abs(revised - adjustment) < 0.005:
            adjustment = revised
            break
        adjustment = revised
    return adjustment


def run_game_simulation(event, trials=TRIALS, seed=None, model_parameters=None):
    if trials != TRIALS:
        raise ValueError("The simulation is fixed at exactly 10,000 trials.")
    if not isinstance(event, dict):
        raise ValueError("A valid stored game event is required.")
    family = _family(event.get("sport_key") or event.get("sport"))
    market_inputs = _collect_market_inputs(event)
    has_complete_scoring_markets = bool(market_inputs["spread_margins"] and market_inputs["totals"])
    if family in ("tennis", "mma") or family is None or not has_complete_scoring_markets:
        if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
            raise ValueError("Simulation seed must be an integer or None.")
        return _run_outcome_only_simulation(event, seed, family)
    expected_margin, expected_total, spread_books, total_books = _market_inputs(event)
    if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
        raise ValueError("Simulation seed must be an integer or None.")
    prior = LEAGUE_PRIORS[family]
    fitted = _validated_model_parameters(model_parameters, family, str(event.get('sport_key') or event.get('sport') or ''))
    empirical_fit = bool(fitted and int(fitted.get("sample_size", 0)) > 0)
    if fitted:
        prior = {**prior, **{key: fitted[key] for key in ("margin_sd", "total_sd") if key in fitted}}
    overtime_total_adjustment = _estimate_overtime_total_adjustment(
        event.get("sport_key") or event.get("sport"), family,
        expected_margin, expected_total, prior, fitted, seed,
    )
    regulation_total_anchor = expected_total - overtime_total_adjustment
    rng = random.Random(seed)
    home_wins = away_wins = ties = over = under = pushes = 0
    home_covers = away_covers = spread_pushes = 0
    regulation_ties = overtime_games = 0
    home_scores, away_scores, margins, game_totals = [], [], [], []
    for _ in range(TRIALS):
        home_score, away_score = _draw_regulation_scores(
            rng, family, expected_margin, regulation_total_anchor, prior, fitted
        )
        if home_score == away_score:
            regulation_ties += 1
            home_score, away_score, went_overtime = _resolve_tie(
                rng, event.get("sport_key") or event.get("sport"), home_score, away_score
            )
            overtime_games += int(went_overtime)
        margin = home_score - away_score
        total = home_score + away_score
        home_scores.append(home_score)
        away_scores.append(away_score)
        margins.append(margin)
        game_totals.append(total)
        if margin > 0:
            home_wins += 1
        elif margin < 0:
            away_wins += 1
        else:
            ties += 1
        # The consensus home spread is the inverse of the home outcome point:
        # a -4.5 home quote means the home team must win by more than 4.5.
        if margin > expected_margin + 1e-9:
            home_covers += 1
        elif margin < expected_margin - 1e-9:
            away_covers += 1
        else:
            spread_pushes += 1
        if total > expected_total:
            over += 1
        elif total < expected_total:
            under += 1
        else:
            pushes += 1

    raw_spread_probabilities = [home_covers / TRIALS, away_covers / TRIALS, spread_pushes / TRIALS]
    spread_temperature = fitted.get("spread_temperature") if fitted else None
    if spread_temperature is not None:
        weights = [max(1e-12, probability) ** (1.0 / spread_temperature)
                   for probability in raw_spread_probabilities]
        weight_total = sum(weights)
        raw_spread_probabilities = [weight / weight_total for weight in weights]
    home_cover_probability = round(raw_spread_probabilities[0] * 100, 2)
    away_cover_probability = round(raw_spread_probabilities[1] * 100, 2)
    spread_push_probability = round(100.0 - home_cover_probability - away_cover_probability, 2)

    raw_probabilities = [home_wins / TRIALS, away_wins / TRIALS, ties / TRIALS]
    temperature = fitted.get("temperature") if fitted else None
    if temperature is not None:
        weights = [max(1e-12, probability) ** (1.0 / temperature)
                   for probability in raw_probabilities]
        weight_total = sum(weights)
        raw_probabilities = [weight / weight_total for weight in weights]
    home_probability = round(raw_probabilities[0] * 100, 2)
    away_probability = round(raw_probabilities[1] * 100, 2)
    tie_probability = round(100.0 - home_probability - away_probability, 2)
    raw_total_probabilities = [over / TRIALS, under / TRIALS, pushes / TRIALS]
    total_temperature = fitted.get("total_temperature") if fitted else None
    if total_temperature is not None:
        weights = [max(1e-12, probability) ** (1.0 / total_temperature)
                   for probability in raw_total_probabilities]
        weight_total = sum(weights)
        raw_total_probabilities = [weight / weight_total for weight in weights]
    over_probability = round(raw_total_probabilities[0] * 100, 2)
    under_probability = round(raw_total_probabilities[1] * 100, 2)
    total_push_probability = round(100.0 - over_probability - under_probability, 2)
    pct = lambda n: round(n / TRIALS * 100, 2)
    return {
        "trials": TRIALS,
        "simulation_seed": seed,
        "event_id": event.get("id"),
        "sport": event.get("sport_key"),
        "matchup": f"{event.get('away_team')} @ {event.get('home_team')}",
        "commence_time": event.get("commence_time"),
        "home_team": event.get("home_team"),
        "away_team": event.get("away_team"),
        "projected_score": {
            "home": round(sum(home_scores) / TRIALS, 2),
            "away": round(sum(away_scores) / TRIALS, 2),
        },
        "simulation_ranges": {
            "home_score": _distribution_summary(home_scores),
            "away_score": _distribution_summary(away_scores),
            "home_margin": _distribution_summary(margins),
            "game_total": _distribution_summary(game_totals),
            "interpretation": (
                "Empirical 10th-to-90th percentile range across simulated final scores; "
                "this describes model outcome spread, not a confidence interval for the true score."
            ),
        },
        "win_probabilities": {
            "home": home_probability, "away": away_probability, "tie": tie_probability,
        },
        "spread_market": {
            "consensus_home_spread": round(-expected_margin, 2),
            "home_cover": home_cover_probability,
            "away_cover": away_cover_probability,
            "push": spread_push_probability,
            "probability_temperature": spread_temperature,
            "settlement_basis": "simulated_final_score",
        },
        "game_resolution": {
            "regulation_ties": regulation_ties, "games_with_overtime_or_extra_innings": overtime_games,
            "expected_overtime_total_adjustment": round(overtime_total_adjustment, 3),
            "regulation_total_anchor": round(regulation_total_anchor, 2),
            "rule": "league_specific_approximation",
        },
        "total_market": {
            "consensus_line": round(expected_total, 2),
            "over": over_probability, "under": under_probability, "push": total_push_probability,
            "probability_temperature": total_temperature,
        },
        "margin_market": {
            "consensus_home_margin": round(expected_margin, 2),
            "home_margin_sd": prior["margin_sd"],
            "model_source": "empirical_training_fit" if empirical_fit else "broad_league_prior",
            "training_sample_size": fitted.get("sample_size") if fitted else 0,
            "fitted_margin_bias": fitted.get("margin_bias") if empirical_fit else None,
            "fitted_total_bias": fitted.get("total_bias") if empirical_fit else None,
            "fitted_margin_total_correlation": fitted.get("margin_total_corr") if empirical_fit else None,
            "probability_temperature": temperature,
        },
        "data_quality": {
            "spread_books": spread_books,
            "total_books": total_books,
            "distribution": prior["distribution"],
            "model_source": "empirical_training_fit" if empirical_fit else "broad_league_prior",
            "training_sample_size": fitted.get("sample_size") if fitted else 0,
            "shared_count_environment_alpha": (
                fitted.get("shared_alpha", 0.0) if fitted and prior["distribution"] == "poisson" else None
            ),
            "market_diagnostics": _collect_market_inputs(event)["diagnostics"],
        },
        "methodology": (
            "Empirically fitted per-league market-residual model: stored sportsbook spread "
            "and total anchor this game; earlier verified training events supply league-specific "
            "residual bias, variance and correlation; count-based leagues use team-specific "
            "overdispersion plus a shared positive scoring-environment factor. This is not a copy "
            "of any sportsbook model and does not ingest team/player efficiency, injuries, "
            "lineups or weather. It is not validated until evaluated on an adequate untouched "
            "chronological holdout."
            if empirical_fit else
            "Market-informed scenario model with broad league-family priors; outcome "
            "probabilities have a separately fitted calibration temperature."
            if temperature is not None else
            "Market-informed scenario model: median complete sportsbook spread and total "
            "quotes anchor expected margin and scoring level; broad league-family priors "
            "generate trial outcomes. It is not a validated forecast."
        ),
    }
