import math
import random
from statistics import median

TRIALS = 10_000

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
    if price == 0:
        raise ValueError("Invalid zero odds")
    return 1 + (price / 100 if price > 0 else 100 / abs(price))


def _market_inputs(event):
    home, away = event.get("home_team", ""), event.get("away_team", "")
    spread_margins, totals = [], []
    for book in event.get("bookmakers", []):
        markets = {m.get("key"): m for m in book.get("markets", [])}
        spread = markets.get("spreads")
        if spread:
            home_lines = [o.get("point") for o in spread.get("outcomes", [])
                          if o.get("name") == home and o.get("point") is not None]
            away_lines = [o.get("point") for o in spread.get("outcomes", [])
                          if o.get("name") == away and o.get("point") is not None]
            if home_lines:
                spread_margins.append(-float(home_lines[0]))
            elif away_lines:
                spread_margins.append(float(away_lines[0]))
        total = markets.get("totals")
        if total:
            points = [float(o["point"]) for o in total.get("outcomes", [])
                      if o.get("point") is not None and o.get("name", "").lower() in ("over", "under")]
            if points:
                totals.append(median(points))
    if not spread_margins or not totals:
        raise ValueError("This game needs a current sportsbook spread and total consensus before it can be simulated.")
    return median(spread_margins), median(totals), len(spread_margins), len(totals)


def _poisson(rng, mean):
    if mean <= 0:
        return 0
    limit = math.exp(-mean)
    product, count = 1.0, 0
    while product > limit:
        count += 1
        product *= rng.random()
    return count - 1


def run_game_simulation(event, trials=TRIALS):
    if trials != TRIALS:
        raise ValueError("The simulation is fixed at exactly 10,000 trials.")
    family = _family(event.get("sport_key") or event.get("sport"))
    if family is None:
        raise ValueError("This league is not yet supported by the game simulator.")
    expected_margin, expected_total, spread_books, total_books = _market_inputs(event)
    prior = LEAGUE_PRIORS[family]
    rng = random.Random()
    home_wins = away_wins = ties = over = under = pushes = 0
    home_scores, away_scores, margins, totals = [], [], [], []
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
        margins.append(margin)
        totals.append(total)
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
        },
        "methodology": (
            "Market-informed simulation: median current sportsbook spread and total "
            "set this game's expected margin and scoring level; league-level scoring "
            "volatility priors generate the 10,000 outcomes. This is not a copy of "
            "any sportsbook's proprietary model and does not currently ingest team "
            "efficiency, injuries, lineups, weather or player-level projections."
        ),
    }
