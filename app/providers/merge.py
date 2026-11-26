"""Deterministic merging of normalized odds from independent providers."""
from __future__ import annotations

from datetime import datetime, timezone
import re


def _team_key(value):
    return "".join(char.lower() for char in str(value or "") if char.isalnum())


def _timestamp(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc).timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _team_abbreviations(value):
    words = re.findall(r"[a-z0-9]+", str(value or "").lower())
    if not words:
        return set()
    variants = {"".join(word[0] for word in words)}
    if len(words) >= 2:
        city, nickname = words[:-1], words[-1]
        variants.add("".join(word[0] for word in city))
        variants.add("".join(word[0] for word in city) + nickname[0])
        variants.add(words[0][:3])
        if len(city) >= 2:
            variants.add(city[0][:2] + city[1][0])
    return {item for item in variants if item}


def _team_matches(left, right):
    a, b = _team_key(left), _team_key(right)
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) <= 4 and len(b) > 4:
        return a in _team_abbreviations(right)
    if len(b) <= 4 and len(a) > 4:
        return b in _team_abbreviations(left)
    return False


def _same_event(left, right, tolerance_seconds=300):
    if left.get("sport_key") != right.get("sport_key"):
        return False
    same_orientation = (
        _team_matches(left.get("home_team"), right.get("home_team"))
        and _team_matches(left.get("away_team"), right.get("away_team"))
    )
    tennis = str(left.get("sport_key") or "").startswith("tennis_")
    swapped_orientation = tennis and (
        _team_matches(left.get("home_team"), right.get("away_team"))
        and _team_matches(left.get("away_team"), right.get("home_team"))
    )
    if not (same_orientation or swapped_orientation):
        return False
    a = _timestamp(left.get("commence_time"))
    b = _timestamp(right.get("commence_time"))
    return a is not None and b is not None and abs(a - b) <= tolerance_seconds


def _canonical_outcome_name(outcome, source_event, target_event):
    name = str(outcome.get("name") or "")
    if _team_matches(name, source_event.get("home_team")):
        if _team_matches(source_event.get("home_team"), target_event.get("home_team")):
            return target_event.get("home_team") or name
        if _team_matches(source_event.get("home_team"), target_event.get("away_team")):
            return target_event.get("away_team") or name
    if _team_matches(name, source_event.get("away_team")):
        if _team_matches(source_event.get("away_team"), target_event.get("away_team")):
            return target_event.get("away_team") or name
        if _team_matches(source_event.get("away_team"), target_event.get("home_team")):
            return target_event.get("home_team") or name
    return name


def _outcome_identity(outcome):
    point = outcome.get("point")
    try:
        point = None if point is None else float(point)
    except (TypeError, ValueError, OverflowError):
        return None
    return (
        str(outcome.get("name") or ""),
        str(outcome.get("description") or outcome.get("participant") or ""),
        point,
    )


def _quote_rank(outcome, provider_rank):
    updated = _timestamp(outcome.get("updated_at") or outcome.get("source_updated_at"))
    return (updated if updated is not None else float("-inf"), -provider_rank)


def _provider_access_class(provider_name, account_group, provider_access_classes, provider_account_access_classes):
    """Resolve tier by provider account; configured providers fail closed for unknown groups."""
    normalized_group = str(account_group or "").strip()
    if not normalized_group:
        return "restricted"
    account_classes = provider_account_access_classes or {}
    if provider_name in account_classes:
        return account_classes[provider_name].get(normalized_group, "restricted")
    access_class = (provider_access_classes or {}).get(provider_name)
    return access_class if access_class in {"free", "premium"} else "restricted"


def merge_provider_events(provider_results, preferred_provider_names=None, provider_access_classes=None, provider_account_access_classes=None):
    """Merge normalized event lists in explicit source-priority order.

    Event identity is strict: sport, ordered home/away identities, and kickoff
    within five minutes. A provider cannot silently reverse home/away. Exact
    event IDs are preferred, then the strict matchup identity. For a duplicate
    book/market/selection/line, the newest provider timestamp wins; when no
    timestamps exist, earlier provider order wins.
    """
    merged = []
    by_id = {}
    by_identity = {}
    provider_rank = {name: index for index, (name, _) in enumerate(provider_results)}
    # A just-refreshed provider wins timestamp ties against retained snapshot data.
    for name in preferred_provider_names or ():
        provider_rank[name] = -1

    for provider_name, events in provider_results:
        for raw_event in events or []:
            if not isinstance(raw_event, dict) or not raw_event.get("id"):
                continue
            event = dict(raw_event)
            raw_books = event.get("bookmakers") or []
            event_id = str(event["id"])
            target = by_id.get((event.get("sport_key"), event_id))
            if target is not None and not _same_event(target, event):
                target = None
            identity = (
                event.get("sport_key"),
                _team_key(event.get("home_team")),
                _team_key(event.get("away_team")),
            )
            if target is None:
                target = next(
                    (candidate for candidate in merged if _same_event(candidate, event)),
                    None,
                )
            if target is None:
                event["id"] = event_id
                event["sources"] = [provider_name]
                event["source"] = provider_name
                event["bookmakers"] = []
                merged.append(event)
                by_id[(event.get("sport_key"), event_id)] = event
                by_identity.setdefault(identity, []).append(event)
                target = event
            else:
                if provider_name not in target["sources"]:
                    target["sources"].append(provider_name)
                target["source"] = "+".join(target["sources"])
                by_id[(event.get("sport_key"), event_id)] = target

            book_map = {book.get("key"): book for book in target["bookmakers"]}
            for raw_book in raw_books:
                if not isinstance(raw_book, dict) or not raw_book.get("key"):
                    continue
                book_key = str(raw_book["key"])
                book = book_map.get(book_key)
                if book is None:
                    book = {
                        "key": book_key,
                        "title": raw_book.get("title") or book_key,
                        "markets": [],
                    }
                    target["bookmakers"].append(book)
                    book_map[book_key] = book
                market_map = {market.get("key"): market for market in book["markets"]}
                for raw_market in raw_book.get("markets") or []:
                    if not isinstance(raw_market, dict) or not raw_market.get("key"):
                        continue
                    market_key = str(raw_market["key"])
                    market = market_map.get(market_key)
                    if market is None:
                        market = {"key": market_key, "outcomes": []}
                        book["markets"].append(market)
                        market_map[market_key] = market
                    outcome_map = {
                        (
                            item.get("source_access_class") or "restricted",
                            (
                                str(item.get("source_provider") or "")
                                if item.get("source_account_group") else ""
                            ),
                            str(item.get("source_account_group") or ""),
                            _outcome_identity(item),
                        ): index
                        for index, item in enumerate(market["outcomes"])
                        if _outcome_identity(item) is not None
                    }
                    for raw_outcome in raw_market.get("outcomes") or []:
                        if not isinstance(raw_outcome, dict):
                            continue
                        outcome_identity = _outcome_identity(raw_outcome)
                        if outcome_identity is None:
                            continue
                        candidate = dict(raw_outcome)
                        candidate["name"] = _canonical_outcome_name(
                            candidate, event, target
                        )
                        candidate["source_provider"] = provider_name
                        candidate["source_account_group"] = str(candidate.get("source_account_group") or "")
                        candidate["source_access_class"] = _provider_access_class(
                            provider_name, candidate["source_account_group"],
                            provider_access_classes, provider_account_access_classes,
                        )
                        identity_key = (
                            candidate["source_access_class"],
                            provider_name if candidate["source_account_group"] else "",
                            candidate["source_account_group"],
                            outcome_identity,
                        )
                        if "size" in candidate:
                            candidate.setdefault("size_source_provider", provider_name)
                            size_provider = str(candidate.get("size_source_provider") or provider_name)
                            size_group = str(candidate.get("size_source_account_group") or candidate["source_account_group"])
                            candidate["size_source_account_group"] = size_group
                            candidate["size_source_access_class"] = (
                                "restricted"
                                if size_provider != provider_name and not size_group
                                else _provider_access_class(
                                    size_provider, size_group, provider_access_classes,
                                    provider_account_access_classes,
                                )
                            )
                        existing_index = outcome_map.get(identity_key)
                        if existing_index is None:
                            outcome_map[identity_key] = len(market["outcomes"])
                            market["outcomes"].append(candidate)
                            continue
                        existing = market["outcomes"][existing_index]
                        existing_provider = existing.get("source_provider", "")
                        if "size" in existing:
                            existing.setdefault("size_source_provider", existing_provider)
                        if _quote_rank(candidate, provider_rank.get(provider_name, 999)) > _quote_rank(
                            existing, provider_rank.get(existing_provider, 999)
                        ):
                            for metadata in ("size", "size_source_provider", "size_source_account_group", "size_source_access_class", "main"):
                                if metadata not in candidate and metadata in existing:
                                    candidate[metadata] = existing[metadata]
                            market["outcomes"][existing_index] = candidate
                        else:
                            for metadata in ("size", "size_source_provider", "size_source_account_group", "main"):
                                if metadata not in existing and metadata in candidate:
                                    existing[metadata] = candidate[metadata]

    return merged



def merge_provider_snapshot(existing_events, refreshed_provider_names, new_provider_results, provider_access_classes=None, provider_account_access_classes=None, refreshed_provider_account_groups=None):
    """Replace refreshed providers while retaining other providers' latest quotes.

    The database stores one current snapshot per sport. A provider-specific
    refresh must therefore carry forward quotes from providers not refreshed
    in this run, while removing the refreshed provider's old quotes before
    adding its new response (including an empty successful response).
    """
    refreshed = set(refreshed_provider_names or ())
    retained = {}

    for raw_event in existing_events or []:
        if not isinstance(raw_event, dict):
            continue
        by_provider = {}
        for raw_book in raw_event.get("bookmakers") or []:
            if not isinstance(raw_book, dict):
                continue
            for raw_market in raw_book.get("markets") or []:
                if not isinstance(raw_market, dict):
                    continue
                for raw_outcome in raw_market.get("outcomes") or []:
                    if not isinstance(raw_outcome, dict):
                        continue
                    provider = str(raw_outcome.get("source_provider") or "legacy")
                    account_group = str(raw_outcome.get("source_account_group") or "")
                    refreshed_groups = (refreshed_provider_account_groups or {}).get(provider)
                    if provider in refreshed and (
                        refreshed_groups is None or account_group in set(refreshed_groups)
                    ):
                        continue
                    book = by_provider.setdefault(provider, {}).setdefault(
                        str(raw_book.get("key") or ""),
                        {"key": str(raw_book.get("key") or ""),
                         "title": raw_book.get("title") or raw_book.get("key") or "",
                         "markets": {}},
                    )
                    market = book["markets"].setdefault(
                        str(raw_market.get("key") or ""),
                        {"key": str(raw_market.get("key") or ""), "outcomes": []},
                    )
                    market["outcomes"].append(dict(raw_outcome))

        for provider, books in by_provider.items():
            event = {key: value for key, value in raw_event.items()
                     if key not in ("bookmakers", "sources", "source")}
            event["bookmakers"] = []
            for book in books.values():
                book_copy = dict(book)
                book_copy["markets"] = list(book["markets"].values())
                event["bookmakers"].append(book_copy)
            retained.setdefault(provider, []).append(event)

    retained_results = [(name, events) for name, events in retained.items()]
    new_results = list(new_provider_results or [])
    merged = merge_provider_events(
        retained_results + new_results,
        preferred_provider_names=refreshed,
        provider_access_classes=provider_access_classes,
        provider_account_access_classes=provider_account_access_classes,
    )
    provider_names = list(dict.fromkeys(
        [name for name, _ in retained_results] +
        [name for name, _ in new_results]
    ))
    return merged, provider_names
