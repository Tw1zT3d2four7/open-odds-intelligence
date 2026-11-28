from collections import defaultdict
from math import isfinite
from statistics import median

from .math import american_to_decimal, no_vig_probabilities


def _records(value):
    """Return list-shaped provider records; ignore malformed container values."""
    return value if isinstance(value, list) else []


def _event_id(event):
    """Return a stable scalar event identity, excluding bools and containers."""
    value = event.get("id")
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    value = str(value).strip()
    return value or None


def _outcome_description(outcome):
    value = outcome.get("description") or outcome.get("participant") or ""
    return value if isinstance(value, str) else ""


def _valid_american_price(value):
    return (not isinstance(value, bool) and isinstance(value, int)
            and value != 0 and not -100 < value < 100)


def _finite_point(value):
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if isfinite(number) else None


def _line_group(market_key, point):
    """Return a finite canonical line, or None when a required line is absent.

    H2H markets have no point. Every other market must carry a usable line;
    grouping missing/NaN lines together can create false consensus or arb.
    """
    if market_key == "h2h":
        return None
    if point is None:
        return None
    try:
        value = float(point)
    except (TypeError, ValueError, OverflowError):
        return None
    if not isfinite(value):
        return None
    # Spread sides carry opposite signed points; compare the same handicap.
    return abs(value) if market_key == "spreads" else value


def market_board(events, best_only=True):
    """Return best prices, or every valid sportsbook quote, per exact selection."""
    grouped = defaultdict(list)
    for event in _records(events):
        if not isinstance(event, dict):
            continue
        event_id = _event_id(event)
        if event_id is None:
            continue
        for book in _records(event.get("bookmakers")):
            if not isinstance(book, dict):
                continue
            book_key = book.get("key")
            if not isinstance(book_key, str) or not book_key.strip():
                continue
            book_key = book_key.strip()
            book_title = book.get("title")
            if not isinstance(book_title, str) or not book_title.strip():
                book_title = book_key
            for market in _records(book.get("markets")):
                if not isinstance(market, dict):
                    continue
                market_key = market.get("key")
                if not isinstance(market_key, str) or not market_key.strip():
                    continue
                market_key = market_key.strip()
                for outcome in _records(market.get("outcomes")):
                    if not isinstance(outcome, dict):
                        continue
                    name, price = outcome.get("name"), outcome.get("price")
                    if not isinstance(name, str) or not name.strip():
                        continue
                    name = name.strip()
                    if not _valid_american_price(price):
                        continue
                    point = _finite_point(outcome.get("point"))
                    line = _line_group(market_key, point)
                    if market_key != "h2h" and line is None:
                        continue
                    description = _outcome_description(outcome)
                    if market_key.startswith("player_") and not description.strip():
                        continue
                    # A spread side is identified by its outcome and
                    # handicap magnitude; providers may sign that handicap
                    # differently while still referring to the same side.
                    key = (event_id, market_key, name, line, description)
                    grouped[key].append({
                        "event_id": event_id, "sport": event.get("sport_title"),
                        "matchup": f'{event.get("away_team", "")} @ {event.get("home_team", "")}',
                        "commence_time": event.get("commence_time") if isinstance(event.get("commence_time"), str) else "",
                        "market": market_key, "outcome": name, "outcome_description": description, "point": point,
                        "bookmaker": book_title,
                        "bookmaker_key": book_key, "price": price,
                    })
    rows = []
    for quotes in grouped.values():
        if best_only:
            rows.append(max(quotes, key=lambda q: american_to_decimal(q["price"])))
        else:
            rows.extend(quotes)
    return sorted(rows, key=lambda row: (
        row["commence_time"] or "", row["matchup"], row["market"],
        row["outcome"], row["point"] if row["point"] is not None else -999
    ))


def analyze_markets(events, min_ev=1.0, min_consensus_books=2, best_only=True, arbitrage_only=False):
    """Calculate leave-one-book-out EV and cross-book two-way arbitrage.

    Only complete, matching outcome sets are compared. Soccer h2h is a
    three-way market (home/draw/away); other supported markets are treated
    as two-way. When books disagree on the outcome set, only the unique
    most-common complete set is eligible for comparison.
    """
    opportunities, arbitrage = [], []
    for event in _records(events):
        if not isinstance(event, dict):
            continue
        event_id = _event_id(event)
        if event_id is None:
            continue
        line_sets = defaultdict(list)
        candidate_sets = defaultdict(lambda: defaultdict(dict))
        sport_key = str(event.get("sport_key") or event.get("sport") or "").lower()

        # Build complete no-vig market sets per sportsbook and exact line.
        for book in _records(event.get("bookmakers")):
            if not isinstance(book, dict):
                continue
            book_key = book.get("key")
            if not isinstance(book_key, str) or not book_key.strip():
                continue
            book_key = book_key.strip()
            for market in _records(book.get("markets")):
                if not isinstance(market, dict):
                    continue
                market_key = market.get("key")
                if not isinstance(market_key, str) or not market_key.strip():
                    continue
                market_key = market_key.strip()
                if arbitrage_only and market_key not in ("h2h", "spreads", "totals"):
                    continue
                expected_outcomes = (
                    3 if market_key == "h2h" and sport_key.startswith("soccer_")
                    else 2
                )
                groups = defaultdict(list)
                for outcome in _records(market.get("outcomes")):
                    if not isinstance(outcome, dict):
                        continue
                    name, price = outcome.get("name"), outcome.get("price")
                    if not isinstance(name, str) or not name.strip():
                        continue
                    name = name.strip()
                    if not _valid_american_price(price):
                        continue
                    player_scope = None
                    if market_key.startswith("player_"):
                        player_scope = _outcome_description(outcome).strip()
                        if not player_scope:
                            continue
                    line = _line_group(market_key, outcome.get("point"))
                    if market_key != "h2h" and line is None:
                        continue
                    groups[(
                        line, player_scope,
                        str(outcome.get("source_provider") or ""),
                        str(outcome.get("source_account_group") or ""),
                        str(outcome.get("source_access_class") or "restricted"),
                    )].append(outcome)

                for (line, player_scope, _source_provider, _source_account_group, _source_access_class), items in groups.items():
                    if len(items) != expected_outcomes:
                        continue
                    # A valid spread pair must describe opposite sides of the
                    # same handicap. Some feeds incorrectly attach the same
                    # signed point to both teams; this can manufacture huge,
                    # impossible arbitrage percentages.
                    if market_key == "spreads":
                        spread_points = [_finite_point(item.get("point")) for item in items]
                        if (any(point is None or point == 0 for point in spread_points)
                                or spread_points[0] * spread_points[1] >= 0):
                            continue
                    identities = [
                        (outcome["name"], _line_group(market_key, outcome.get("point")),
                         _outcome_description(outcome))
                        for outcome in items
                    ]
                    if len(set(identities)) != expected_outcomes:
                        continue
                    signature = tuple(sorted(identities))
                    probabilities = no_vig_probabilities([o["price"] for o in items])
                    quotes = []
                    for outcome, identity, probability in zip(items, identities, probabilities):
                        quotes.append({
                            "book": book, "outcome": outcome,
                            "identity": identity, "probability": probability,
                        })
                    # One canonical complete set per book / market / line.
                    source_identity = (
                        book_key,
                        str(items[0].get("source_provider") or ""),
                        str(items[0].get("source_account_group") or ""),
                        str(items[0].get("source_access_class") or "restricted"),
                    )
                    candidate_sets[(market_key, line, player_scope)][signature][source_identity] = quotes

        # Exclude incomplete or mismatched sets. A tied modal signature is
        # ambiguous, so fail closed rather than mixing different market types.
        for market_line, signatures in candidate_sets.items():
            counts = [(len({source[0] for source in sources}), signature)
                      for signature, sources in signatures.items()]
            if not counts:
                continue
            highest = max(count for count, _ in counts)
            winners = [signature for count, signature in counts if count == highest]
            if len(winners) != 1:
                continue
            selected = signatures[winners[0]]
            for book_quotes in selected.values():
                line_sets[market_line].extend(book_quotes)

        if not arbitrage_only:
            # EV: median of other books' no-vig probabilities for the same outcome/line.
            best = {}
            for (market_key, line, _player_scope), quotes in line_sets.items():
                # Index peer probabilities once per selection. The previous
                # implementation rescanned every quote for every quote,
                # making large multi-book markets quadratic in quote count.
                peer_index = defaultdict(lambda: defaultdict(list))
                identity_access = defaultdict(set)
                for item in quotes:
                    book_key = item["book"].get("key")
                    if book_key:
                        peer_index[item["identity"]][book_key].append(item["probability"])
                    identity_access[item["identity"]].add(
                        item["outcome"].get("source_access_class")
                    )
                peer_medians = {
                    identity: {book_key: median(values) for book_key, values in books.items()}
                    for identity, books in peer_index.items()
                }
                derived_access = {}
                for identity, access_classes in identity_access.items():
                    if any(value not in {"free", "premium"} for value in access_classes):
                        derived_access[identity] = "restricted"
                    elif "premium" in access_classes:
                        derived_access[identity] = "premium"
                    else:
                        derived_access[identity] = "free"

                for quote in quotes:
                    own_book = quote["book"].get("key")
                    peers = [
                        probability for book_key, probability in peer_medians.get(quote["identity"], {}).items()
                        if book_key != own_book
                    ]
                    if len(peers) < min_consensus_books:
                        continue
                    # Each sportsbook contributes once, regardless of the
                    # number of account groups it has in the source feed.
                    fair = median(peers)
                    price = quote["outcome"]["price"]
                    ev = (fair * american_to_decimal(price) - 1) * 100
                    if ev < min_ev:
                        continue
                    identity = (market_key, line, quote["identity"])
                    derived_access_class = derived_access.get(quote["identity"], "restricted")
                    row = {
                        "source_access_class": derived_access_class,
                        "source_provider": quote["outcome"].get("source_provider") or "",
                        "source_account_group": quote["outcome"].get("source_account_group") or "",
                        "event_id": event_id, "sport": event.get("sport_title"),
                        "sport_key": event.get("sport_key") or event.get("sport"),
                        "matchup": f'{event.get("away_team", "")} @ {event.get("home_team", "")}',
                        "commence_time": event.get("commence_time"), "market": market_key,
                        "outcome": quote["outcome"]["name"],
                        "outcome_description": _outcome_description(quote["outcome"]),
                        "point": quote["outcome"].get("point"),
                        "bookmaker": quote["book"].get("title", quote["book"].get("key")),
                        "bookmaker_key": quote["book"].get("key"), "price": price,
                        "fair_probability": round(fair, 6), "consensus_books": len(peers),
                        "ev_percent": round(ev, 2),
                    }
                    if best_only:
                        if identity not in best or american_to_decimal(price) > american_to_decimal(best[identity]["price"]):
                            best[identity] = row
                    else:
                        # CLV needs each qualifying sportsbook's first quote,
                        # not only the best price selected for the public board.
                        opportunities.append(row)
            if best_only:
                opportunities.extend(best.values())

        # Arbitrage: only complete two-outcome markets, with distinct books.
        for (market_key, line, _player_scope), quotes in line_sets.items():
            if market_key.startswith("player_"):
                continue
            sides = defaultdict(list)
            for quote in quotes:
                sides[quote["identity"]].append(quote)
            if len(sides) != 2:
                continue
            side_items = list(sides.values())
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
            implied_sum, first, second, first_decimal, second_decimal = min(
                pairs, key=lambda pair: pair[0]
            )
            if implied_sum >= 1:
                continue
            side_quotes = [first, second]
            decimals = [first_decimal, second_decimal]
            arbitrage.append({
                "event_id": event_id, "sport": event.get("sport_title"),
                "matchup": f'{event.get("away_team", "")} @ {event.get("home_team", "")}',
                "commence_time": event.get("commence_time"), "market": market_key,
                "line": line, "arb_percent": round((1 - implied_sum) * 100, 3),
                "legs": [{
                    "outcome": q["outcome"]["name"], "point": q["outcome"].get("point"),
                    "bookmaker": q["book"].get("title", q["book"].get("key")),
                    "bookmaker_key": q["book"].get("key"), "price": q["outcome"]["price"],
                    "stake_percent": round((1 / decimal) / implied_sum * 100, 2),
                } for q, decimal in zip(side_quotes, decimals)],
            })
    opportunities.sort(key=lambda row: row["ev_percent"], reverse=True)
    arbitrage.sort(key=lambda row: row["arb_percent"], reverse=True)
    return {"opportunities": opportunities, "arbitrage": arbitrage}
