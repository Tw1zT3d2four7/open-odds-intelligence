"""Free-tier posted sportsbook limit monitor using OddsLink's published API.

Only explicit size.kind == "posted_limit" values are stored. Exchange depth,
contract counts, inferred limits and missing values are deliberately excluded.
"""
import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from . import config
from .providers.key_pool import NoAvailableKeys, ProviderKeyPool, parse_api_key_entries
from .providers.oddslink import OddsLinkProvider
from .providers.the_odds_api import ProviderError


SPORT_MAP = {
    "americanfootball_nfl": "nfl",
    "americanfootball_ncaaf": "ncaaf",
    "basketball_nba": "nba",
    "basketball_wnba": "wnba",
    "basketball_ncaab": "ncaab",
    "icehockey_nhl": "nhl",
    "baseball_mlb": "mlb",
    "soccer_epl": "epl",
    "soccer_usa_mls": "mls",
}

DEFAULT_LIMIT_SPORTS = (
    "americanfootball_nfl", "basketball_nba", "icehockey_nhl",
    "baseball_mlb", "soccer_epl",
)


def selected_limit_sports(configured_sports, limit_sports):
    """Choose limit polling leagues without silently expanding free-tier usage."""
    if limit_sports:
        return list(limit_sports)
    return [sport for sport in DEFAULT_LIMIT_SPORTS if sport in configured_sports]


class LimitsError(RuntimeError):
    pass


def oddslink_key_entries():
    entries = parse_api_key_entries(config.ODDSLINK_API_KEYS)
    if config.ODDSLINK_API_KEY and not any(
        key == config.ODDSLINK_API_KEY for _, key in entries
    ):
        entries.append(("default", config.ODDSLINK_API_KEY))
    return entries


def oddslink_api_key():
    entries = oddslink_key_entries()
    return entries[0][1] if entries else ""


def oddslink_keyset_fingerprint():
    entries = oddslink_key_entries()
    material = "\x00".join(f"{group}\x00{key}" for group, key in entries)
    return hashlib.sha256(material.encode("utf-8")).hexdigest() if material else ""


def _eligible_oddslink_account_groups(configured_groups=None):
    allowed_classes = {"free"}
    if config.ODDS_PREMIUM_PROVIDER_POLLING_ENABLED:
        allowed_classes.add("premium")
    if configured_groups is None:
        configured_groups = {group for group, _ in oddslink_key_entries()}
    overrides = config.ODDS_PROVIDER_ACCOUNT_ACCESS_CLASSES.get("oddslink")
    if overrides is not None:
        return {
            group for group, access_class in overrides.items()
            if group in configured_groups and access_class in allowed_classes
        }
    provider_class = config.ODDS_PROVIDER_ACCESS_CLASSES.get("oddslink", "restricted")
    if provider_class not in allowed_classes:
        return set()
    return set(configured_groups)


class OddsLinkClient:
    """Quota-aware, multi-key wrapper for the standalone market-limit poller."""

    def __init__(self, api_key=None):
        entries = parse_api_key_entries(config.ODDSLINK_API_KEYS)
        if config.ODDSLINK_API_KEY and not any(
            key == config.ODDSLINK_API_KEY for _, key in entries
        ):
            entries.append(("default", config.ODDSLINK_API_KEY))
        if not entries and api_key:
            entries = [("default", api_key)]
        unique = []
        seen = set()
        for group, key in entries:
            if key and key not in seen:
                unique.append((group, key))
                seen.add(key)
        if not unique:
            raise LimitsError("ODDSLINK_API_KEY or ODDSLINK_API_KEYS is not configured")
        self.pool = ProviderKeyPool(
            "oddslink_limits", [f"{group}|{key}" for group, key in unique]
        )
        self.allowed_account_groups = _eligible_oddslink_account_groups(
            {group for group, _ in unique}
        )
        self.key_count = len(unique)
        self.last_account_group = ""
        self.last_access_class = "restricted"
        self.usage = {}
        self.last_key_fingerprint = ""

    def fetch(self, sport):
        if not self.allowed_account_groups:
            raise LimitsError("No OddsLink account groups are eligible for market-limit polling")
        last_error = None
        for _ in range(self.key_count):
            try:
                lease = self.pool.acquire(allowed_account_groups=self.allowed_account_groups)
            except NoAvailableKeys as exc:
                last_error = exc
                break
            provider = OddsLinkProvider(lease.key)
            try:
                remaining = provider.fetch_usage()
                if remaining is not None and remaining <= 0:
                    reset = provider._seconds_until(provider.usage.get("period_resets_at"))
                    raise ProviderError(
                        "OUT_OF_USAGE_CREDITS: OddsLink free monthly allowance is exhausted",
                        status_code=429,
                        retry_after=str(reset or 86400),
                        retryable=False,
                    )
                payload = provider._request(
                    f"/sports/{sport}/odds",
                    {"bookmakers": "circa,pinnacle", "markets": "h2h,spreads,totals"},
                )
            except ProviderError as exc:
                self.pool.mark_failure(
                    lease,
                    status_code=exc.status_code,
                    message=str(exc),
                    retry_after_seconds=exc.retry_after,
                )
                last_error = exc
                continue
            self.pool.mark_success(lease)
            self.pool.record_usage(
                lease, provider.usage,
                minimum_remaining=config.ODDS_PROVIDER_ACCOUNT_QUOTA_FLOORS.get(
                    "oddslink", {}
                ).get(lease.account_group, {}),
            )
            self.usage = dict(provider.usage)
            self.last_key_fingerprint = lease.fingerprint
            self.last_account_group = lease.account_group
            account_classes = config.ODDS_PROVIDER_ACCOUNT_ACCESS_CLASSES.get("oddslink")
            self.last_access_class = (
                account_classes.get(lease.account_group, "restricted")
                if account_classes is not None
                else config.ODDS_PROVIDER_ACCESS_CLASSES.get("oddslink", "restricted")
            )
            if self.last_access_class not in ("free", "premium"):
                self.last_access_class = "restricted"
            if isinstance(payload, dict) and payload.get("error"):
                error = payload["error"]
                message = (error.get("message") or error.get("code") or str(error)
                           if isinstance(error, dict) else str(error))
                raise LimitsError(f"OddsLink API error: {message}")
            if isinstance(payload, list):
                events = payload
            elif isinstance(payload, dict) and isinstance(payload.get("events"), list):
                events = payload["events"]
            else:
                raise LimitsError("OddsLink returned an unexpected events response format")
            for event in events:
                if not isinstance(event, dict):
                    continue
                for book in event.get("bookmakers") or []:
                    if not isinstance(book, dict):
                        continue
                    for market in book.get("markets") or []:
                        if not isinstance(market, dict):
                            continue
                        for outcome in market.get("outcomes") or []:
                            if isinstance(outcome, dict):
                                outcome["source_provider"] = "oddslink"
                                outcome["source_account_group"] = lease.account_group
                                outcome["source_access_class"] = self.last_access_class
            return events
        raise LimitsError(str(last_error or "No available OddsLink API keys"))


class MarketLimitsMonitor:
    def __init__(self, path=None):
        self.path = str(path or config.DATABASE_PATH)
        self._refresh_lock = threading.Lock()
        self.last_error = None
        self.last_updated = None
        self.usage = {}
        self.last_changes = []
        self.last_diagnostic = {}
        self._initialize()
        with self._connect() as db:
            saved_success = db.execute(
                "SELECT state_value FROM market_limit_state WHERE state_key='last_success'"
            ).fetchone()
            saved_usage = db.execute(
                "SELECT state_value FROM market_limit_state WHERE state_key='last_usage'"
            ).fetchone()
            saved_usage_key = db.execute(
                "SELECT state_value FROM market_limit_state WHERE state_key='last_usage_key_fingerprint'"
            ).fetchone()
            saved_error = db.execute(
                "SELECT state_value FROM market_limit_state WHERE state_key='last_error'"
            ).fetchone()
        self.last_updated = saved_success["state_value"] if saved_success else None
        self.last_error = saved_error["state_value"] if saved_error else None
        current_key_fingerprint = (
            oddslink_keyset_fingerprint() or ""
        )
        if saved_usage and saved_usage_key and saved_usage_key["state_value"] == current_key_fingerprint:
            try:
                self.usage = json.loads(saved_usage["state_value"])
            except (TypeError, ValueError):
                self.usage = {}

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def _initialize(self):
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS market_limit_quotes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    observed_at TEXT NOT NULL,
                    sport TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    commence_time TEXT NOT NULL DEFAULT '',
                    home_team TEXT NOT NULL DEFAULT '',
                    away_team TEXT NOT NULL DEFAULT '',
                    bookmaker_key TEXT NOT NULL,
                    bookmaker_title TEXT NOT NULL DEFAULT '',
                    market_key TEXT NOT NULL,
                    outcome_name TEXT NOT NULL,
                    point REAL,
                    price REAL,
                    limit_amount REAL NOT NULL,
                    currency TEXT NOT NULL DEFAULT 'bookmaker_currency',
                    source_provider TEXT NOT NULL DEFAULT '',
                    source_account_group TEXT NOT NULL DEFAULT '',
                    source_access_class TEXT NOT NULL DEFAULT 'restricted',
                    source_capability TEXT NOT NULL DEFAULT 'posted_market_limits',
                    UNIQUE(observed_at,event_id,bookmaker_key,market_key,outcome_name,point,source_provider,source_account_group,source_access_class,source_capability)
                );
                CREATE INDEX IF NOT EXISTS idx_limit_lookup
                ON market_limit_quotes(event_id,bookmaker_key,market_key,outcome_name,point,id);
                CREATE TABLE IF NOT EXISTS market_limit_state (
                    state_key TEXT PRIMARY KEY,
                    state_value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sent_alerts (
                    fingerprint TEXT PRIMARY KEY,
                    sent_at TEXT NOT NULL,
                    alert_type TEXT NOT NULL,
                    summary TEXT NOT NULL
                );
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(market_limit_quotes)")}
            for column, declaration in (
                ("source_provider", "TEXT NOT NULL DEFAULT ''"),
                ("source_account_group", "TEXT NOT NULL DEFAULT ''"),
                ("source_access_class", "TEXT NOT NULL DEFAULT 'restricted'"),
                ("source_capability", "TEXT NOT NULL DEFAULT 'posted_market_limits'"),
            ):
                if column not in columns:
                    db.execute(f"ALTER TABLE market_limit_quotes ADD COLUMN {column} {declaration}")

            expected_unique = [
                "observed_at", "event_id", "bookmaker_key", "market_key",
                "outcome_name", "point", "source_provider",
                "source_account_group", "source_access_class", "source_capability",
            ]
            scoped_unique = False
            for index in db.execute("PRAGMA index_list(market_limit_quotes)").fetchall():
                if not index["unique"]:
                    continue
                index_columns = [
                    row["name"] for row in db.execute(
                        f"PRAGMA index_info('{index['name']}')"
                    ).fetchall()
                ]
                if index_columns == expected_unique:
                    scoped_unique = True
                    break
            if not scoped_unique:
                db.execute("DROP TABLE IF EXISTS market_limit_quotes_scoped_migration")
                db.execute("""
                    CREATE TABLE market_limit_quotes_scoped_migration (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        observed_at TEXT NOT NULL,
                        sport TEXT NOT NULL,
                        event_id TEXT NOT NULL,
                        commence_time TEXT NOT NULL DEFAULT '',
                        home_team TEXT NOT NULL DEFAULT '',
                        away_team TEXT NOT NULL DEFAULT '',
                        bookmaker_key TEXT NOT NULL,
                        bookmaker_title TEXT NOT NULL DEFAULT '',
                        market_key TEXT NOT NULL,
                        outcome_name TEXT NOT NULL,
                        point REAL,
                        price REAL,
                        limit_amount REAL NOT NULL,
                        currency TEXT NOT NULL DEFAULT 'bookmaker_currency',
                        source_provider TEXT NOT NULL DEFAULT '',
                        source_account_group TEXT NOT NULL DEFAULT '',
                        source_access_class TEXT NOT NULL DEFAULT 'restricted',
                        source_capability TEXT NOT NULL DEFAULT 'posted_market_limits',
                        UNIQUE(observed_at,event_id,bookmaker_key,market_key,outcome_name,point,
                               source_provider,source_account_group,source_access_class,source_capability)
                    )
                """)
                db.execute("""
                    INSERT OR IGNORE INTO market_limit_quotes_scoped_migration(
                        id,observed_at,sport,event_id,commence_time,home_team,away_team,
                        bookmaker_key,bookmaker_title,market_key,outcome_name,point,price,
                        limit_amount,currency,source_provider,source_account_group,
                        source_access_class,source_capability
                    )
                    SELECT id,observed_at,sport,event_id,commence_time,home_team,away_team,
                        bookmaker_key,bookmaker_title,market_key,outcome_name,point,price,
                        limit_amount,currency,source_provider,source_account_group,
                        'restricted',source_capability
                    FROM market_limit_quotes
                """)
                db.execute("DROP TABLE market_limit_quotes")
                db.execute(
                    "ALTER TABLE market_limit_quotes_scoped_migration RENAME TO market_limit_quotes"
                )
                db.execute("""
                    CREATE INDEX IF NOT EXISTS idx_limit_lookup
                    ON market_limit_quotes(event_id,bookmaker_key,market_key,outcome_name,point,id)
                """)

    @staticmethod
    def _extract(sport, events, observed_at, *, source_provider="oddslink",
                 source_account_group="", source_access_class="restricted",
                 source_capability="posted_market_limits"):
        import math

        rows = []
        if not isinstance(events, list):
            return rows
        for event in events:
            if not isinstance(event, dict):
                continue
            raw_event_id = event.get("id")
            if isinstance(raw_event_id, bool) or not isinstance(raw_event_id, (str, int)):
                continue
            event_id = str(raw_event_id).strip()
            if not event_id:
                continue
            books = event.get("bookmakers")
            if not isinstance(books, list):
                continue
            for book in books:
                if not isinstance(book, dict):
                    continue
                markets = book.get("markets")
                if not isinstance(markets, list):
                    continue
                for market in markets:
                    if not isinstance(market, dict):
                        continue
                    outcomes = market.get("outcomes")
                    if not isinstance(outcomes, list):
                        continue
                    for outcome in outcomes:
                        if not isinstance(outcome, dict):
                            continue
                        size = outcome.get("size")
                        if not isinstance(size, dict) or size.get("kind") != "posted_limit":
                            continue
                        raw_amount = size.get("amount")
                        if isinstance(raw_amount, bool):
                            continue
                        try:
                            amount = float(raw_amount)
                        except (TypeError, ValueError, OverflowError):
                            continue
                        if not math.isfinite(amount) or amount <= 0:
                            continue
                        raw_price = outcome.get("price")
                        if raw_price is None:
                            price = None
                        else:
                            if isinstance(raw_price, bool):
                                continue
                            try:
                                price = float(raw_price)
                            except (TypeError, ValueError, OverflowError):
                                continue
                            if (not math.isfinite(price) or not price.is_integer()
                                    or price == 0 or -100 < price < 100):
                                continue
                            price = int(price)
                        point = outcome.get("point")
                        if point is not None:
                            if isinstance(point, bool) or not isinstance(point, (int, float)):
                                continue
                            try:
                                point = float(point)
                            except (TypeError, ValueError, OverflowError):
                                continue
                            if not math.isfinite(point):
                                continue
                        book_key, market_key, outcome_name = (
                            book.get("key"), market.get("key"), outcome.get("name")
                        )
                        if not all(isinstance(value, str) and value.strip()
                                   for value in (book_key, market_key, outcome_name)):
                            continue
                        metadata = (
                            event.get("commence_time", ""),
                            event.get("home_team", ""),
                            event.get("away_team", ""),
                        )
                        if not all(isinstance(value, str) for value in metadata):
                            continue
                        title = book.get("title")
                        if not isinstance(title, str) or not title.strip():
                            title = book_key
                        row_provider = str(
                            outcome.get("size_source_provider")
                            or outcome.get("source_provider") or source_provider or ""
                        )
                        # Limit amounts come from size metadata, so their tier
                        # must follow the size source rather than the odds quote.
                        row_group = str(
                            outcome.get("size_source_account_group")
                            or outcome.get("source_account_group") or ""
                        )
                        row_class = (
                            outcome.get("size_source_access_class")
                            or outcome.get("source_access_class")
                            or source_access_class
                        )
                        if not row_provider or not row_group or row_class not in ("free", "premium"):
                            row_class = "restricted"
                        rows.append((
                            observed_at, sport, event_id,
                            *metadata, book_key, title, market_key, outcome_name,
                            point, price, amount, str(row_provider or ""),
                            str(row_group or ""), row_class,
                            str(source_capability or "posted_market_limits"),
                        ))

        # SQLite UNIQUE constraints treat NULL values as distinct. Deduplicate
        # this refresh batch explicitly so H2H observations (which have no line)
        # cannot be inserted or alerted more than once for the same identity.
        unique_rows = {}
        conflicting = set()
        for row in rows:
            identity = (row[0], row[2], row[6], row[8], row[9], row[10], row[13], row[14], row[15], row[16])
            previous = unique_rows.get(identity)
            if previous is not None and previous != row:
                conflicting.add(identity)
            else:
                unique_rows[identity] = row
        return [row for identity, row in unique_rows.items() if identity not in conflicting]

    def _store_rows(self, rows):
        changes = []
        with self._connect() as db:
            for row in rows:
                previous = db.execute("""
                    SELECT limit_amount,observed_at FROM market_limit_quotes
                    WHERE event_id=? AND bookmaker_key=? AND market_key=?
                      AND outcome_name=? AND point IS ? AND source_provider=?
                      AND source_account_group=? AND source_access_class=? AND source_capability=?
                    ORDER BY id DESC LIMIT 1
                """, (row[2], row[6], row[8], row[9], row[10], row[13], row[14], row[15], row[16])).fetchone()
                db.execute("""
                    INSERT OR IGNORE INTO market_limit_quotes
                    (observed_at,sport,event_id,commence_time,home_team,away_team,
                     bookmaker_key,bookmaker_title,market_key,outcome_name,point,price,limit_amount,
                     source_provider,source_account_group,source_access_class,source_capability)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, row)
                if previous and float(previous["limit_amount"]) != row[12]:
                    old, new = float(previous["limit_amount"]), row[12]
                    changes.append({
                        "observed_at": row[0], "sport": row[1],
                        "event_id": row[2], "matchup": f"{row[5]} @ {row[4]}",
                        "commence_time": row[3], "bookmaker_key": row[6],
                        "bookmaker": row[7], "market": row[8], "outcome": row[9],
                        "point": row[10], "price": row[11],
                        "previous_limit": old, "limit_amount": new,
                        "change_amount": new - old,
                        "change_percent": round((new - old) / old * 100, 2) if old else None,
                        "direction": "increased" if new > old else "decreased",
                    })
            db.commit()
        self.last_changes = changes
        self._send_alerts(changes)
        return changes

    def ingest_events(self, events, *, observed_at=None, usage=None, key_fingerprint=None,
                      source_provider="oddslink", source_access_class="restricted",
                      source_capability="posted_market_limits"):
        """Record posted limits from an already-fetched OddsLink odds snapshot."""
        if not self._refresh_lock.acquire(blocking=False):
            return {"ok": False, "skipped": True, "error": "A market-limits update is already running.", "observations": 0}
        try:
            observed_at = observed_at or datetime.now(timezone.utc).isoformat()
            by_sport = {}
            for event in events or []:
                if isinstance(event, dict) and event.get("sport_key"):
                    by_sport.setdefault(event["sport_key"], []).append(event)
            rows = []
            for sport, sport_events in by_sport.items():
                rows.extend(self._extract(
                    sport, sport_events, observed_at,
                    source_provider=source_provider,
                    source_account_group=key_fingerprint or "",
                    source_access_class=source_access_class,
                    source_capability=source_capability,
                ))
            changes = self._store_rows(rows)
            self.usage = dict(usage or self.usage)
            self.last_updated = observed_at
            self.last_error = None
            self.last_diagnostic = {
                "source": "multi_provider_oddslink_snapshot",
                "sports_received": list(by_sport),
                "events_returned": sum(len(value) for value in by_sport.values()),
                "posted_limit_outcomes": len(rows),
            }
            with self._connect() as db:
                db.execute("DELETE FROM market_limit_state WHERE state_key='last_error'")
                db.execute(
                    "INSERT OR REPLACE INTO market_limit_state(state_key,state_value) VALUES('last_attempt',?)",
                    (observed_at,),
                )
                db.execute(
                    "INSERT OR REPLACE INTO market_limit_state(state_key,state_value) VALUES('last_success',?)",
                    (observed_at,),
                )
                if usage is not None:
                    db.execute(
                        "INSERT OR REPLACE INTO market_limit_state(state_key,state_value) VALUES('last_usage',?)",
                        (json.dumps(self.usage),),
                    )
                if key_fingerprint:
                    db.execute(
                        "INSERT OR REPLACE INTO market_limit_state(state_key,state_value) VALUES('last_key_fingerprint',?)",
                        (key_fingerprint,),
                    )
                    db.execute(
                        "INSERT OR REPLACE INTO market_limit_state(state_key,state_value) VALUES('last_usage_key_fingerprint',?)",
                        (key_fingerprint,),
                    )
            return {
                "ok": True, "source": "stored_oddslink_snapshot",
                "observations": len(rows), "changes": changes,
                "updated_at": self.last_updated, "usage": self.usage,
                "diagnostic": self.last_diagnostic,
            }
        finally:
            self._refresh_lock.release()

    def refresh(self, force=False):
        # A startup poll and a manual dashboard refresh must not duplicate
        # provider calls while a previous limits batch is still running.
        if not self._refresh_lock.acquire(blocking=False):
            return {"ok": False, "skipped": True,
                    "error": "A market-limits refresh is already running; this request was skipped to protect provider quota.",
                    "observations": 0}
        try:
            return self._refresh_locked(force=force)
        finally:
            self._refresh_lock.release()

    def _refresh_locked(self, force=False):
        api_key = oddslink_api_key()
        if not api_key:
            self.last_error = "Market limits feed is disabled. Add a free OddsLink API key as ODDSLINK_API_KEY or ODDSLINK_API_KEYS."
            return {"ok": False, "error": self.last_error, "observations": 0}
        if not _eligible_oddslink_account_groups():
            self.last_error = "No OddsLink account groups are eligible for market-limit polling."
            return {"ok": False, "error": self.last_error, "observations": 0}
        now = datetime.now(timezone.utc)
        observed_at = now.isoformat()
        # Persist the last attempt so container restarts cannot reset the
        # free-tier daily-default polling guard. BEGIN IMMEDIATE also coordinates
        # multiple application workers sharing the same SQLite database.
        with self._connect() as state_db:
            state_db.execute("BEGIN IMMEDIATE")
            previous = state_db.execute(
                "SELECT state_value FROM market_limit_state WHERE state_key='last_attempt'"
            ).fetchone()
            previous_key = state_db.execute(
                "SELECT state_value FROM market_limit_state WHERE state_key='last_key_fingerprint'"
            ).fetchone()
            current_key_fingerprint = oddslink_keyset_fingerprint()
            key_changed = bool(
                previous_key and previous_key["state_value"] != current_key_fingerprint
            )
            if previous and not force and not key_changed:
                try:
                    last_attempt = datetime.fromisoformat(previous["state_value"].replace("Z", "+00:00"))
                    remaining = config.LIMITS_REFRESH_INTERVAL_SECONDS - (
                        now - last_attempt.astimezone(timezone.utc)
                    ).total_seconds()
                except (TypeError, ValueError):
                    remaining = 0
                if remaining > 0:
                    return {
                        "ok": True, "skipped": True, "reason": "refresh_cooldown",
                        "next_refresh_seconds": int(remaining) + 1,
                        "updated_at": self.last_updated, "observations": 0,
                        "message": "Market limits refresh is being held to the configured quota-safe interval.",
                    }
            state_db.execute(
                "INSERT OR REPLACE INTO market_limit_state(state_key,state_value) VALUES('last_attempt',?)",
                (observed_at,),
            )
            state_db.execute(
                "INSERT OR REPLACE INTO market_limit_state(state_key,state_value) VALUES('last_key_fingerprint',?)",
                (current_key_fingerprint,),
            )
        client = OddsLinkClient(api_key)
        all_rows, errors = [], []
        diagnostic = {"sports_requested": [], "events_returned": 0, "bookmakers_returned": [], "size_kinds_returned": {}, "posted_limit_outcomes": 0}
        sports = selected_limit_sports(config.ODDS_SPORTS, config.LIMITS_SPORTS)
        for configured_sport in sports:
            sport = SPORT_MAP.get(configured_sport)
            if not sport:
                continue
            diagnostic["sports_requested"].append(sport)
            try:
                events = client.fetch(sport)
                diagnostic["events_returned"] += len(events)
                for event in events:
                    if not isinstance(event, dict):
                        continue
                    books = event.get("bookmakers")
                    if not isinstance(books, list):
                        continue
                    for book in books:
                        if not isinstance(book, dict):
                            continue
                        key = str(book.get("key", "unknown"))
                        if key not in diagnostic["bookmakers_returned"]:
                            diagnostic["bookmakers_returned"].append(key)
                        markets = book.get("markets")
                        if not isinstance(markets, list):
                            continue
                        for market in markets:
                            if not isinstance(market, dict):
                                continue
                            outcomes = market.get("outcomes")
                            if not isinstance(outcomes, list):
                                continue
                            for outcome in outcomes:
                                if not isinstance(outcome, dict):
                                    continue
                                size = outcome.get("size")
                                kind = str(size.get("kind", "no_size")) if isinstance(size, dict) else "no_size"
                                diagnostic["size_kinds_returned"][kind] = diagnostic["size_kinds_returned"].get(kind, 0) + 1
                                if kind == "posted_limit":
                                    diagnostic["posted_limit_outcomes"] += 1
                all_rows.extend(self._extract(
                    configured_sport, events, observed_at,
                    source_provider="oddslink",
                    source_account_group=getattr(client, "last_account_group", current_key_fingerprint),
                    source_access_class=getattr(
                        client, "last_access_class",
                        config.ODDS_PROVIDER_ACCESS_CLASSES.get("oddslink", "restricted"),
                    ),
                    source_capability="posted_market_limits",
                ))
            except LimitsError as exc:
                errors.append(f"{sport}: {exc}")
                # Stop immediately for quota, authentication or rate-limit
                # failures, including provider errors returned in HTTP 200 bodies.
                error_text = str(exc).upper()
                stop_tokens = (
                    "HTTP 401", "HTTP 403", "HTTP 429", "OUT_OF_USAGE_CREDITS",
                    "QUOTA", "RATE LIMIT", "RATE_LIMIT", "UNAUTHORIZED", "FORBIDDEN",
                )
                if any(token in error_text for token in stop_tokens):
                    break
        changes = []
        with self._connect() as db:
            for row in all_rows:
                previous = db.execute("""
                    SELECT limit_amount,observed_at FROM market_limit_quotes
                    WHERE event_id=? AND bookmaker_key=? AND market_key=?
                      AND outcome_name=? AND point IS ? AND source_provider=?
                      AND source_account_group=? AND source_access_class=?
                    ORDER BY id DESC LIMIT 1
                """, (row[2], row[6], row[8], row[9], row[10], row[13], row[14], row[15])).fetchone()
                db.execute("""
                    INSERT OR IGNORE INTO market_limit_quotes
                    (observed_at,sport,event_id,commence_time,home_team,away_team,
                     bookmaker_key,bookmaker_title,market_key,outcome_name,point,price,limit_amount,
                     source_provider,source_account_group,source_access_class,source_capability)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, row)
                if previous and float(previous["limit_amount"]) != row[12]:
                    old, new = float(previous["limit_amount"]), row[12]
                    change = {
                        "observed_at": observed_at, "sport": row[1],
                        "event_id": row[2], "matchup": f"{row[5]} @ {row[4]}",
                        "commence_time": row[3], "bookmaker_key": row[6],
                        "bookmaker": row[7], "market": row[8], "outcome": row[9],
                        "point": row[10], "price": row[11],
                        "previous_limit": old, "limit_amount": new,
                        "change_amount": new - old,
                        "change_percent": round((new - old) / old * 100, 2) if old else None,
                        "direction": "increased" if new > old else "decreased",
                    }
                    changes.append(change)
            db.commit()
        self.usage = dict(client.usage)
        self.last_diagnostic = diagnostic
        self.last_error = "; ".join(errors)[:1000] if errors else None
        with self._connect() as state_db:
            if self.last_error:
                state_db.execute(
                    "INSERT OR REPLACE INTO market_limit_state(state_key,state_value) VALUES('last_error',?)",
                    (self.last_error,),
                )
            else:
                state_db.execute("DELETE FROM market_limit_state WHERE state_key='last_error'")
            state_db.execute(
                "INSERT OR REPLACE INTO market_limit_state(state_key,state_value) VALUES('last_usage',?)",
                (json.dumps(self.usage),),
            )
            state_db.execute(
                "INSERT OR REPLACE INTO market_limit_state(state_key,state_value) VALUES('last_usage_key_fingerprint',?)",
                (current_key_fingerprint,),
            )
            if not errors or all_rows:
                self.last_updated = observed_at
                state_db.execute(
                    "INSERT OR REPLACE INTO market_limit_state(state_key,state_value) VALUES('last_success',?)",
                    (observed_at,),
                )
        self.last_changes = changes
        self._send_alerts(changes)
        return {"ok": not errors, "observations": len(all_rows),
                "changes": changes, "updated_at": self.last_updated,
                "error": self.last_error, "usage": self.usage,
                "diagnostic": diagnostic}

    def _send_alerts(self, changes):
        if not config.DISCORD_WEBHOOK_URL:
            return
        from urllib.request import Request
        for change in changes:
            if change["direction"] != "increased" and not config.LIMITS_ALERT_DECREASES:
                continue
            identity = json.dumps([
                change["event_id"], change["bookmaker_key"], change["market"],
                change["outcome"], change["point"], change["previous_limit"],
                change["limit_amount"], change["observed_at"],
            ], sort_keys=True)
            direction = change["direction"].upper()
            point = change.get("point")
            selection = change.get("outcome") or "Selection"
            if point is not None:
                selection = f"{selection} ({point:+g})"
            commence = change.get("commence_time")
            try:
                parsed = datetime.fromisoformat(str(commence).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                game_time = f"<t:{int(parsed.timestamp())}:F> (<t:{int(parsed.timestamp())}:R>)"
            except (TypeError, ValueError, OverflowError, OSError):
                game_time = "Time unavailable"
            change_percent = change.get("change_percent")
            percent_text = f"{change_percent:+.2f}%" if isinstance(change_percent, (int, float)) else "Unavailable"
            message = (
                f"MARKET LIMIT {direction} · {change.get('bookmaker', 'Unknown book')} · "
                f"{change.get('matchup', 'Matchup unavailable')} · {change.get('market', 'Market')} · {selection}"
            )
            embed = {
                "title": f"MARKET LIMIT {direction}",
                "description": f"**{change.get('matchup') or 'Matchup unavailable'}**",
                "color": 0x2ECC71 if direction == "INCREASED" else 0xE67E22,
                "fields": [
                    {"name": "Sportsbook", "value": str(change.get("bookmaker") or "Unknown book"), "inline": True},
                    {"name": "Market", "value": str(change.get("market") or "Market"), "inline": True},
                    {"name": "Selection", "value": selection, "inline": True},
                    {"name": "Previous posted limit", "value": f"{change['previous_limit']:,.0f}", "inline": True},
                    {"name": "New posted limit", "value": f"{change['limit_amount']:,.0f}", "inline": True},
                    {"name": "Change", "value": f"{change.get('change_amount', 0):+,.0f} ({percent_text})", "inline": True},
                    {"name": "Game time", "value": game_time, "inline": False},
                ],
                "footer": {"text": "Provider-reported posted market maximum · Currency follows the sportsbook · Not account-specific"},
            }
            fingerprint = hashlib.sha256(identity.encode()).hexdigest()
            with self._connect() as db:
                exists = db.execute(
                    "SELECT 1 FROM sent_alerts WHERE fingerprint=?", (fingerprint,)
                ).fetchone()
                if exists:
                    continue
                db.execute("INSERT INTO sent_alerts VALUES(?,?,?,?)",
                           (fingerprint, change["observed_at"], "market_limit", message))
                db.commit()
            try:
                request = Request(
                    config.DISCORD_WEBHOOK_URL,
                    data=json.dumps({"embeds": [embed], "allowed_mentions": {"parse": []}}).encode(),
                    headers={"Content-Type": "application/json",
                             "User-Agent": "OpenOddsIntelligence/0.5"},
                    method="POST",
                )
                with urlopen(request, timeout=8) as response:
                    if response.status not in (200, 204):
                        raise RuntimeError(f"Discord HTTP {response.status}")
            except Exception:
                with self._connect() as db:
                    db.execute("DELETE FROM sent_alerts WHERE fingerprint=?", (fingerprint,))
                    db.commit()

    def status(self):
        with self._connect() as db:
            count = db.execute("SELECT COUNT(*) FROM market_limit_quotes").fetchone()[0]
            books = [r[0] for r in db.execute(
                "SELECT DISTINCT bookmaker_key FROM market_limit_quotes ORDER BY 1"
            )]
            state = db.execute(
                "SELECT state_value FROM market_limit_state WHERE state_key='last_attempt'"
            ).fetchone()
            last_key_state = db.execute(
                "SELECT state_value FROM market_limit_state WHERE state_key='last_key_fingerprint'"
            ).fetchone()
        last_attempt = state["state_value"] if state else None
        current_key_fingerprint = oddslink_keyset_fingerprint() or ""
        key_changed = bool(
            last_key_state and last_key_state["state_value"] != current_key_fingerprint
        )
        next_refresh_seconds = 0
        if last_attempt and not key_changed:
            try:
                attempted = datetime.fromisoformat(last_attempt.replace("Z", "+00:00"))
                next_refresh_seconds = max(0, int(
                    config.LIMITS_REFRESH_INTERVAL_SECONDS -
                    (datetime.now(timezone.utc) - attempted.astimezone(timezone.utc)).total_seconds()
                ))
            except (TypeError, ValueError):
                next_refresh_seconds = 0
        return {
            "enabled": bool(oddslink_api_key()),
            "provider": "OddsLink Free",
            "last_updated": self.last_updated,
            "last_attempt": last_attempt,
            "next_refresh_seconds": next_refresh_seconds,
            "last_error": self.last_error,
            "observations": count,
            "books_with_posted_limits": books,
            "requests_remaining": self.usage.get("requests_remaining"),
            "requests_used": self.usage.get("requests_used"),
            "refresh_interval_seconds": config.LIMITS_REFRESH_INTERVAL_SECONDS,
            "free_plan_monthly_request_allowance": 1000,
            "diagnostic": self.last_diagnostic,
        }

    def current(self, sport=None, limit=500, include_premium=None):
        """Return latest limits after applying the caller's provider-data scope."""
        sql = """SELECT q.* FROM market_limit_quotes q JOIN (
            SELECT event_id,bookmaker_key,market_key,outcome_name,point,
                   source_provider,source_account_group,source_access_class,source_capability,MAX(id) id
            FROM market_limit_quotes"""
        args = []
        if include_premium is False:
            sql += " WHERE source_access_class='free'"
        elif include_premium is True:
            sql += " WHERE source_access_class IN ('free','premium')"
        sql += " GROUP BY event_id,bookmaker_key,market_key,outcome_name,point,source_provider,source_account_group,source_access_class,source_capability) x ON x.id=q.id"
        if sport:
            sql += " WHERE q.sport=?"
            args.append(sport)
        sql += " ORDER BY q.commence_time,q.bookmaker_key,q.market_key LIMIT ?"
        args.append(limit)
        with self._connect() as db:
            return [dict(r) for r in db.execute(sql, args).fetchall()]

    def changes(self, sport=None, direction="increased", limit=200, include_premium=None):
        """Return changes within each provider source, scoped before window aggregation."""
        sql = """WITH scoped AS (
            SELECT * FROM market_limit_quotes"""
        args = []
        if include_premium is False:
            sql += " WHERE source_access_class='free'"
        elif include_premium is True:
            sql += " WHERE source_access_class IN ('free','premium')"
        sql += "),\n        sequenced AS (\n            SELECT q.*,\n                   LAG(limit_amount) OVER (\n                       PARTITION BY event_id,bookmaker_key,market_key,outcome_name,point,\n                                    source_provider,source_account_group,source_access_class,source_capability\n                       ORDER BY id\n                   ) AS previous_limit\n            FROM scoped q\n        )\n        SELECT * FROM sequenced\n        WHERE previous_limit IS NOT NULL AND limit_amount != previous_limit"
        if sport:
            sql += " AND sport=?"
            args.append(sport)
        sql += " ORDER BY id DESC"
        with self._connect() as db:
            rows = [dict(r) for r in db.execute(sql, args).fetchall()]
        results = []
        for row in rows:
            old, new = float(row["previous_limit"]), float(row["limit_amount"])
            change_direction = "increased" if new > old else "decreased"
            if direction in ("increased", "decreased") and change_direction != direction:
                continue
            row.update({
                "matchup": f'{row["away_team"]} @ {row["home_team"]}',
                "previous_limit": old,
                "change_amount": new - old,
                "change_percent": round((new - old) / old * 100, 2) if old else None,
                "direction": change_direction,
                "currency": row.get("currency") or "bookmaker_currency",
            })
            results.append(row)
            if len(results) >= limit:
                break
        return results

    def history(self, event_id, bookmaker, market, outcome, point=None, limit=500, include_premium=None):
        sql = """SELECT * FROM market_limit_quotes
                 WHERE event_id=? AND bookmaker_key=? AND market_key=? AND outcome_name=?"""
        args = [event_id, bookmaker, market, outcome]
        if include_premium is False:
            sql += " AND source_access_class='free'"
        elif include_premium is True:
            sql += " AND source_access_class IN ('free','premium')"
        sql += " AND point IS NULL" if point is None else " AND point=?"
        if point is not None:
            args.append(point)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._connect() as db:
            return [dict(r) for r in db.execute(sql, args).fetchall()]

limits_monitor = MarketLimitsMonitor()
