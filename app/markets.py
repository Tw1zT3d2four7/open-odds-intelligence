from collections import defaultdict


def market_board(events):
    """Return best available quote for each exact market/outcome/line identity.

    Lines are never mixed: e.g. Over 44.5 is not compared with Over 45.5.
    """
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
                    key = (event.get("id"), market_key, name, point)
                    grouped[key].append({
                        "event_id": event.get("id"), "sport": event.get("sport_title"),
                        "matchup": f'{event.get("away_team", "")} @ {event.get("home_team", "")}',
                        "commence_time": event.get("commence_time"),
                        "market": market_key, "outcome": name, "point": point,
                        "bookmaker": book.get("title", book.get("key")),
                        "bookmaker_key": book.get("key"), "price": price,
                    })
    rows = []
    for quotes in grouped.values():
        # American odds are monotonic in decimal payout, but compare decimal odds
        # to avoid treating positive and negative prices as ordinary integers.
        from .math import american_to_decimal
        best = max(quotes, key=lambda q: american_to_decimal(q["price"]))
        rows.append(best)
    return sorted(rows, key=lambda row: (
        row["commence_time"] or "", row["matchup"], row["market"],
        row["outcome"], row["point"] if row["point"] is not None else -999
    ))
