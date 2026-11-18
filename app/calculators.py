"""Pure, quota-free betting math used by the dashboard calculators.

All monetary values are in the user's chosen currency. Odds calculations use
decimal odds internally and never request sportsbook/provider data.
"""
from __future__ import annotations

import math


def _positive(value: float, name: str) -> float:
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite number greater than zero")
    return value


def american_to_decimal(odds: float) -> float:
    odds = float(odds)
    if not math.isfinite(odds) or odds == 0:
        raise ValueError("American odds must be a finite non-zero number")
    return 1 + (odds / 100 if odds > 0 else 100 / abs(odds))


def decimal_to_american(odds: float) -> int:
    odds = _positive(odds, "Decimal odds")
    if odds <= 1:
        raise ValueError("Decimal odds must be greater than 1.00")
    raw = (odds - 1) * 100 if odds >= 2 else -100 / (odds - 1)
    return int(round(raw))


def to_decimal(odds: float, format: str = "american") -> float:
    if format == "american":
        return american_to_decimal(odds)
    if format == "decimal":
        odds = _positive(odds, "Decimal odds")
        if odds <= 1:
            raise ValueError("Decimal odds must be greater than 1.00")
        return odds
    raise ValueError("Odds format must be 'american' or 'decimal'")


def convert_odds(odds: float, from_format: str, to_format: str) -> float | int:
    decimal = to_decimal(odds, from_format)
    if to_format == "decimal":
        return round(decimal, 6)
    if to_format == "american":
        return decimal_to_american(decimal)
    raise ValueError("Odds format must be 'american' or 'decimal'")


def payout(stake: float, odds: float, format: str = "american") -> dict:
    stake = _positive(stake, "Stake")
    decimal = to_decimal(odds, format)
    profit = stake * (decimal - 1)
    return {
        "stake": round(stake, 2), "profit": round(profit, 2),
        "total_return": round(stake * decimal, 2),
        "break_even_probability": round(1 / decimal, 8),
        "decimal_odds": round(decimal, 6),
        "american_odds": decimal_to_american(decimal),
    }


def parlay(legs: list[float], format: str = "american") -> dict:
    if not legs:
        raise ValueError("Add at least one parlay leg")
    combined = 1.0
    for leg in legs:
        combined *= to_decimal(leg, format)
    return {
        "legs": len(legs), "decimal_odds": round(combined, 8),
        "american_odds": decimal_to_american(combined),
        "implied_probability": round(1 / combined, 8),
    }


def no_vig(first_odds: float, second_odds: float,
           format: str = "american") -> dict:
    first_decimal = to_decimal(first_odds, format)
    second_decimal = to_decimal(second_odds, format)
    first_raw, second_raw = 1 / first_decimal, 1 / second_decimal
    overround = first_raw + second_raw
    first_fair, second_fair = first_raw / overround, second_raw / overround
    return {
        "overround": round((overround - 1) * 100, 6),
        "first_implied_probability": round(first_raw, 8),
        "second_implied_probability": round(second_raw, 8),
        "first_fair_probability": round(first_fair, 8),
        "second_fair_probability": round(second_fair, 8),
        "first_fair_decimal": round(1 / first_fair, 6),
        "second_fair_decimal": round(1 / second_fair, 6),
        "first_fair_american": decimal_to_american(1 / first_fair),
        "second_fair_american": decimal_to_american(1 / second_fair),
    }


def ev_kelly(odds: float, win_probability: float, bankroll: float,
             kelly_fraction: float = 0.25, format: str = "american") -> dict:
    decimal = to_decimal(odds, format)
    bankroll = _positive(bankroll, "Bankroll")
    probability = float(win_probability)
    fraction = float(kelly_fraction)
    if not math.isfinite(probability) or not 0 <= probability <= 1:
        raise ValueError("Win probability must be between 0 and 1")
    if not math.isfinite(fraction) or not 0 <= fraction <= 1:
        raise ValueError("Kelly fraction must be between 0 and 1")
    net = decimal - 1
    full_kelly = max(0.0, (probability * decimal - 1) / net)
    sized_fraction = full_kelly * fraction
    return {
        "ev_per_100": round(100 * (probability * net - (1 - probability)), 2),
        "edge_percentage_points": round((probability - 1 / decimal) * 100, 4),
        "implied_probability": round(1 / decimal, 8),
        "full_kelly_fraction": round(full_kelly, 8),
        "fractional_kelly_fraction": round(sized_fraction, 8),
        "full_kelly_stake": round(bankroll * full_kelly, 2),
        "fractional_kelly_stake": round(bankroll * sized_fraction, 2),
    }


def hedge(original_stake: float, original_odds: float, hedge_odds: float,
          format: str = "american") -> dict:
    stake = _positive(original_stake, "Original stake")
    original_decimal = to_decimal(original_odds, format)
    hedge_decimal = to_decimal(hedge_odds, format)
    hedge_stake = stake * original_decimal / hedge_decimal
    locked_profit = stake * (original_decimal - 1) - hedge_stake
    opposite_profit = hedge_stake * (hedge_decimal - 1) - stake
    return {
        "hedge_stake": round(hedge_stake, 2),
        "total_staked": round(stake + hedge_stake, 2),
        "profit_if_original_wins": round(locked_profit, 2),
        "profit_if_hedge_wins": round(opposite_profit, 2),
        "locked_profit": round((locked_profit + opposite_profit) / 2, 2),
    }
