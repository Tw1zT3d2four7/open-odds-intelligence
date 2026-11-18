from collections import defaultdict
from statistics import median

from .math import american_to_decimal, no_vig_probabilities


def _line_group(market_key, point):
    if market_key == "h2h":
        return None
    if point is None:
        return None
    # Spread sides carry opposite signed points; compare the same handicap.
    if market_key == "spreads":
        return abs(float(point))
    return float(point)


def market_board(events):
    """Best quote for each exact market/outcome/line identity."""
    grouped = defaultdict(list)
    for event in events:
        for book in event.get("bookmakers", []):
            for market in book.get("markets", []):
                market_key = market.get("key")
                if not market_key:
                    continue
                for outcome in market.get("outcomes", []):
                    name, price = outcome.get("name"), outcome.get("price")
                    if not name or not isinstance(price, int) or price == 0:
                        continue
                    point = outcome.get("point")
                    description = outcome.get("description") or outcome.get("participant") or ""
                    # A spread side is identified by its outcome and
                    # handicap magnitude; providers may sign that handicap
                    # differently while still referring to the same side.
                    key = (event.get("id"), market_key, name,
                           _line_group(market_key, point), description)
                    grouped[key].append({
                        "event_id": event.get("id"), "sport": event.get("sport_title"),
                        "matchup": f'{event.get("away_team", "")} @ {event.get("home_team", "")}',
                        "commence_time": event.get("commence_time"),
                        "market": market_key, "outcome": name, "outcome_description": description, "point": point,
                        "bookmaker": book.get("title", book.get("key")),
                        "bookmaker_key": book.get("key"), "price": price,
                    })
    rows = []
    for quotes in grouped.values():
        rows.append(max(quotes, key=lambda q: american_to_decimal(q["price"])))
    return sorted(rows, key=lambda row: (
        row["commence_time"] or "", row["matchup"], row["market"],
        row["outcome"], row["point"] if row["point"] is not None else -999
    ))


def analyze_markets(events, min_ev=1.0, min_consensus_books=2):
    """Calculate leave-one-book-out EV and cross-book two-way arbitrage.

    Fair probabilities are formed only from complete two-or-more-outcome
    markets at the same line. A quote is not evaluated against its own book.
    """
    opportunities, arbitrage = [], []
    for event in events:
        # Each book contributes a no-vig probability set for a specific line.
        line_sets = defaultdict(list)
        for book in event.get("bookmakers", []):
            for market in book.get("markets", []):
                market_key = market.get("key")
                if not market_key:
                    continue
                outcomes = market.get("outcomes", [])
                valid = [o for o in outcomes if o.get("name") and
                         isinstance(o.get("price"), int) and o.get("price") != 0]
                if len(valid) < 2:
                    continue
                groups = defaultdict(list)
                for outcome in valid:
                    groups[_line_group(market_key, outcome.get("point"))].append(outcome)
                for line, items in groups.items():
                    if len(items) < 2:
                        continue
                    probs = no_vig_probabilities([o["price"] for o in items])
                    for outcome, probability in zip(items, probs):
                        identity = (
                            outcome["name"],
                            _line_group(market_key, outcome.get("point")),
                            outcome.get("description") or outcome.get("participant") or "",
                        )
                        line_sets[(market_key, line)].append({
                            "book": book, "outcome": outcome,
                            "identity": identity, "probability": probability,
                        })

        # EV: median of other books' no-vig probabilities for the same outcome/line.
        best = {}
        for (market_key, line), quotes in line_sets.items():
            for quote in quotes:
                peers = [q["probability"] for q in quotes
                         if q["book"].get("key") != quote["book"].get("key")
                         and q["identity"] == quote["identity"]]
                peer_books = {q["book"].get("key") for q in quotes
                              if q["book"].get("key") != quote["book"].get("key")
                              and q["identity"] == quote["identity"]}
                if len(peer_books) < min_consensus_books:
                    continue
                fair = median(peers)
                price = quote["outcome"]["price"]
                ev = (fair * american_to_decimal(price) - 1) * 100
                if ev < min_ev:
                    continue
                identity = (market_key, line, quote["identity"])
                row = {
                    "event_id": event.get("id"), "sport": event.get("sport_title"),
                    "matchup": f'{event.get("away_team", "")} @ {event.get("home_team", "")}',
                    "commence_time": event.get("commence_time"), "market": market_key,
                    "outcome": quote["outcome"]["name"],
                    "outcome_description": quote["outcome"].get("description") or quote["outcome"].get("participant") or "",
                    "point": quote["outcome"].get("point"),
                    "bookmaker": quote["book"].get("title", quote["book"].get("key")),
                    "bookmaker_key": quote["book"].get("key"), "price": price,
                    "fair_probability": round(fair, 6), "consensus_books": len(peer_books),
                    "ev_percent": round(ev, 2),
                }
                if identity not in best or american_to_decimal(price) > american_to_decimal(best[identity]["price"]):
                    best[identity] = row
        opportunities.extend(best.values())

        # Arbitrage: best price for every side of a true two-outcome line.
        for (market_key_line, quotes) in line_sets.items():
            market_key, line = market_key_line
            # Player props are evaluated per athlete for EV; keep them out of
            # the two-outcome game-market arbitrage scanner.
            if market_key.startswith("player_"):
                continue
            sides = defaultdict(list)
            for quote in quotes:
                sides[quote["identity"]].append(quote)
            if len(sides) != 2:
                continue
            side_items = list(sides.values())
            # Require distinct books for the two legs. Selecting the best price
            # independently can otherwise report a same-book pair as cross-book.
            pairs = []
            for first in side_items[0]:
                for second in side_items[1]:
                    first_book = first["book"].get("key")
                    second_book = second["book"].get("key")
                    if not first_book or not second_book or first_book == second_book:
                        continue
                    first_decimal = american_to_decimal(first["outcome"]["price"])
                    second_decimal = american_to_decimal(second["outcome"]["price"])
                    implied = 1 / first_decimal + 1 / second_decimal
                    pairs.append((implied, first, second, first_decimal, second_decimal))
            if not pairs:
                continue
            implied_sum, first, second, first_decimal, second_decimal = min(pairs, key=lambda p: p[0])
            if implied_sum >= 1:
                continue
            side_quotes = [first, second]
            decimals = [first_decimal, second_decimal]
            arbitrage.append({
                "event_id": event.get("id"), "sport": event.get("sport_title"),
                "matchup": f'{event.get("away_team", "")} @ {event.get("home_team", "")}',
                "commence_time": event.get("commence_time"), "market": market_key,
                "line": line, "arb_percent": round((1 - implied_sum) * 100, 3),
                "legs": [{
                    "outcome": q["outcome"]["name"], "point": q["outcome"].get("point"),
                    "bookmaker": q["book"].get("title", q["book"].get("key")),
                    "bookmaker_key": q["book"].get("key"), "price": q["outcome"]["price"],
                    "stake_percent": round((1 / d) / implied_sum * 100, 2),
                } for q, d in zip(side_quotes, decimals)],
            })
    opportunities.sort(key=lambda x: x["ev_percent"], reverse=True)
    arbitrage.sort(key=lambda x: x["arb_percent"], reverse=True)
    return {"opportunities": opportunities, "arbitrage": arbitrage}
