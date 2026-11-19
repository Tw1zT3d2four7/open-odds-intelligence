import math
import random
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
}


def _family(sport_key):
    key = str(sport_key or "").lower()
    for token, family in SPORT_FAMILY.items():
        if token in key:
            return family
    return None


def _decimal(price):
    price = float(price)
    if not math.isfinite(price) or -100 < price < 100:
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


def market_readiness(event):
    """Report whether stored quotes contain complete, internally consistent markets."""
    inputs = _collect_market_inputs(event)
    has_spread = bool(inputs["spread_margins"])
    has_total = bool(inputs["totals"])
    missing = []
    if not has_spread:
        missing.append("a complete, valid spread with matching home and away outcomes")
    if not has_total:
        missing.append("a complete, valid total with matching Over and Under outcomes")
    return {
        "can_simulate": has_spread and has_total,
        "has_spread": has_spread,
        "has_total": has_total,
        "spread_books": len(inputs["spread_books"]),
        "total_books": len(inputs["total_books"]),
        "reason": None if not missing else "Missing " + " and ".join(missing) + ".",
        "market_diagnostics": inputs["diagnostics"],
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


def run_game_simulation(event, trials=TRIALS, seed=None, model_parameters=None):
    if trials != TRIALS:
        raise ValueError("The simulation is fixed at exactly 10,000 trials.")
    if not isinstance(event, dict):
        raise ValueError("A valid stored game event is required.")
    family = _family(event.get("sport_key") or event.get("sport"))
    if family is None:
        raise ValueError("This league is not yet supported by the game simulator.")
    expected_margin, expected_total, spread_books, total_books = _market_inputs(event)
    if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
        raise ValueError("Simulation seed must be an integer or None.")
    prior = LEAGUE_PRIORS[family]
    fitted = _validated_model_parameters(model_parameters, family)
    if fitted:
        prior = {**prior, **{key: fitted[key] for key in ("margin_sd", "total_sd") if key in fitted}}
    rng = random.Random(seed)
    home_wins = away_wins = ties = over = under = pushes = 0
    home_scores, away_scores = [], []
    for _ in range(TRIALS):
        if prior["distribution"] == "poisson":
            home_mean = max(0.05, (expected_total + expected_margin) / 2)
            away_mean = max(0.05, (expected_total - expected_margin) / 2)
            home_score = _poisson(rng, home_mean)
            away_score = _poisson(rng, away_mean)
        else:
            margin = rng.gauss(expected_margin, prior["margin_sd"])
            total = max(0.0, rng.gauss(expected_total, prior["total_sd"]))
            home_score = max(0, round((total + margin) / 2))
            away_score = max(0, round((total - margin) / 2))
        margin = home_score - away_score
        total = home_score + away_score
        home_scores.append(home_score)
        away_scores.append(away_score)
        if margin > 0:
            home_wins += 1
        elif margin < 0:
            away_wins += 1
        else:
            ties += 1
        if total > expected_total:
            over += 1
        elif total < expected_total:
            under += 1
        else:
            pushes += 1

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
        "win_probabilities": {
            "home": pct(home_wins), "away": pct(away_wins), "tie": pct(ties),
        },
        "total_market": {
            "consensus_line": round(expected_total, 2),
            "over": pct(over), "under": pct(under), "push": pct(pushes),
        },
        "margin_market": {
            "consensus_home_margin": round(expected_margin, 2),
            "home_margin_sd": prior["margin_sd"],
        },
        "data_quality": {
            "spread_books": spread_books,
            "total_books": total_books,
            "distribution": prior["distribution"],
            "market_diagnostics": _collect_market_inputs(event)["diagnostics"],
        },
        "methodology": (
            "Market-informed simulation: median complete, internally consistent "
            "sportsbook spread pairs and Over/Under total pairs set this game's "
            "expected margin and scoring level; league-level scoring volatility "
            "priors generate the 10,000 outcomes. This is not a copy of any "
            "sportsbook's proprietary model and does not currently ingest team "
            "efficiency, injuries, lineups, weather or player-level projections. "
            "The result is a market-based scenario model, not a validated forecast."
        ),
    }
