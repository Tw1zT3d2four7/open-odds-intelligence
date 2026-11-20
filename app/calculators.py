"""Pure, quota-free betting math used by the dashboard calculators.

All monetary values are in the user's chosen currency. Odds calculations use
decimal odds internally and never request sportsbook/provider data.
"""
from __future__ import annotations

import math


def _finite_number(value: float, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be a finite number") from None
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return number


def _positive(value: float, name: str) -> float:
    value = _finite_number(value, name)
    if value <= 0:
        raise ValueError(f"{name} must be a finite number greater than zero")
    return value


def american_to_decimal(odds: float) -> float:
    if isinstance(odds, bool):
        raise ValueError("American odds must be a finite non-zero number")
    try:
        odds = float(odds)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("American odds must be a finite non-zero number") from None
    if not math.isfinite(odds) or odds == 0:
        raise ValueError("American odds must be a finite non-zero number")
    if not odds.is_integer():
        raise ValueError("American odds must be a whole number")
    if -100 < odds < 100:
        raise ValueError("American odds must be +100 or greater, or -100 or lower")
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
    probability = _finite_number(win_probability, "Win probability")
    fraction = _finite_number(kelly_fraction, "Kelly fraction")
    if not 0 <= probability <= 1:
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



def run_line_minus_one(stake: float, odds: float, margin: float,
                       format: str = "american") -> dict:
    """Settle a -1 run/puck-line wager using the selected team's final margin.

    A win by 2+ covers, a win by exactly 1 pushes, and a tie/loss loses.
    The margin must reflect the sportsbook's settlement rules (including or
    excluding overtime as applicable).
    """
    stake = _positive(stake, "Stake")
    decimal = to_decimal(odds, format)
    margin = _finite_number(margin, "Final score margin")
    if not margin.is_integer():
        raise ValueError("Final score margin must be a whole number")
    if margin > 1:
        result = "win"
        profit = stake * (decimal - 1)
        total_return = stake * decimal
    elif margin == 1:
        result = "push"
        profit = 0.0
        total_return = stake
    else:
        result = "loss"
        profit = -stake
        total_return = 0.0
    return {
        "handicap": -1,
        "result": result,
        "stake": round(stake, 2),
        "profit": round(profit, 2),
        "total_return": round(total_return, 2),
        "decimal_odds": round(decimal, 6),
        "american_odds": decimal_to_american(decimal),
    }


def synthetic_minus_one(moneyline_team: float, moneyline_opponent: float,
                        handicap_team: float, handicap_opponent: float,
                        format: str = "american") -> dict:
    """Estimate a fair -1 price from matching two-way moneyline and -1.5 markets.

    Each market is de-vigged independently. The difference between the
    selected team's fair moneyline win probability and fair -1.5 cover
    probability estimates the one-run/one-goal push probability.
    """
    ml = no_vig(moneyline_team, moneyline_opponent, format)
    handicap = no_vig(handicap_team, handicap_opponent, format)
    win_probability = ml["first_fair_probability"]
    cover_probability = handicap["first_fair_probability"]
    push_probability = win_probability - cover_probability
    loss_probability = 1.0 - win_probability
    if cover_probability <= 0 or cover_probability >= 1:
        raise ValueError("The -1.5 cover probability must be between 0% and 100%")
    if push_probability < -1e-9:
        raise ValueError(
            "Inconsistent prices: the selected team's fair -1.5 probability "
            "cannot exceed its fair moneyline win probability"
        )
    push_probability = max(0.0, push_probability)
    # A -1 wager pushes on an exact one-goal/run win. Pushes return the
    # stake and therefore are excluded from the win/loss break-even equation:
    # p_cover * (decimal - 1) - p_loss = 0.
    fair_decimal = 1.0 + loss_probability / cover_probability
    decisive_probability = cover_probability / (cover_probability + loss_probability)
    return {
        "fair_decimal": round(fair_decimal, 6),
        "fair_american": decimal_to_american(fair_decimal),
        "cover_probability": round(cover_probability, 8),
        "push_probability": round(push_probability, 8),
        "loss_probability": round(loss_probability, 8),
        "decisive_win_probability": round(decisive_probability, 8),
        "moneyline_fair_probability": round(win_probability, 8),
        "moneyline_overround_percent": ml["overround"],
        "handicap_overround_percent": handicap["overround"],
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
