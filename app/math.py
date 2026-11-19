from collections import defaultdict
from statistics import median
import math


def _valid_american_price(price):
    if isinstance(price, bool):
        raise ValueError("American odds must be a finite non-zero number")
    try:
        value = float(price)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("American odds must be a finite non-zero number") from None
    if not math.isfinite(value) or value == 0:
        raise ValueError("American odds must be a finite non-zero number")
    return value


def american_to_decimal(price: int) -> float:
    price = _valid_american_price(price)
    return 1 + (price / 100 if price > 0 else 100 / abs(price))


def american_implied_probability(price: int) -> float:
    price = _valid_american_price(price)
    return 100 / (price + 100) if price > 0 else abs(price) / (abs(price) + 100)

def decimal_to_american(decimal: float) -> int:
    if decimal <= 1:
        raise ValueError("Decimal odds must be greater than 1")
    return round((decimal - 1) * 100) if decimal >= 2 else round(-100 / (decimal - 1))

def no_vig_probabilities(prices: list[int]) -> list[float]:
    if len(prices) < 2:
        return []
    raw = [american_implied_probability(p) for p in prices]
    total = sum(raw)
    return [p / total for p in raw] if total > 0 else []

def expected_value_percent(price: int, fair_probability: float) -> float:
    if not 0 < fair_probability < 1:
        raise ValueError("Fair probability must be between 0 and 1")
    return (fair_probability * american_to_decimal(price) - 1) * 100

def two_way_arbitrage(prices: list[int]) -> dict | None:
    if len(prices) != 2:
        return None
    implied = [1 / american_to_decimal(p) for p in prices]
    total = sum(implied)
    if total >= 1:
        return None
    return {"arb_percent": (1 - total) * 100,
            "stake_percentages": [p / total * 100 for p in implied],
            "implied_sum": total}

def market_consensus(
    events: list[dict],
    market_key: str = "h2h",
    exclude_bookmaker_key: str | None = None,
) -> dict:
    """Build robust no-vig consensus, optionally excluding one sportsbook.

    Excluding the evaluated sportsbook prevents its own quote from feeding the
    fair-probability estimate used to calculate its expected value.
    """
    result = {}
    for event in events:
        accum = defaultdict(list)
        for book in event.get("bookmakers", []):
            if exclude_bookmaker_key and book.get("key") == exclude_bookmaker_key:
                continue
            market = next((m for m in book.get("markets", []) if m.get("key") == market_key), None)
            if not market:
                continue
            outcomes = market.get("outcomes", [])
            if len(outcomes) < 2:
                continue
            names = [o.get("name") for o in outcomes]
            prices = [o.get("price") for o in outcomes]
            if any(not name for name in names) or any(
                not isinstance(price, int) or price == 0 for price in prices
            ):
                continue
            probs = no_vig_probabilities(prices)
            for name, probability in zip(names, probs):
                accum[name].append(probability)
        result[event["id"]] = {
            name: median(values) for name, values in accum.items() if values
        }
    return result
