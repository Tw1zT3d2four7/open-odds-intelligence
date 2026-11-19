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


class LimitsError(RuntimeError):
    pass


class OddsLinkClient:
    BASE = "https://oddslink.app/v1"

    def __init__(self, api_key):
        if not api_key:
            raise LimitsError("ODDSLINK_API_KEY is not configured")
        self.api_key = api_key
        self.usage = {}

    def fetch(self, sport):
        params = urlencode({"bookmakers": "circa,pinnacle",
                            "markets": "h2h,spreads,totals"})
        request = Request(
            f"{self.BASE}/sports/{sport}/odds?{params}",
            headers={"X-API-Key": self.api_key,
                     "Accept": "application/json",
                     "User-Agent": "OpenOddsIntelligence/0.5"},
        )
        try:
            with urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if isinstance(payload, dict) and payload.get("error"):
                    error = payload["error"]
                    message = error.get("message") or error.get("code") or str(error) if isinstance(error, dict) else str(error)
                    raise LimitsError(f"OddsLink API error: {message}")
                for header, key in (
                    ("x-requests-remaining", "requests_remaining"),
                    ("x-requests-used", "requests_used"),
                    ("x-requests-last", "requests_last"),
                ):
                    if response.headers.get(header) is not None:
                        self.usage[key] = response.headers[header]
                return payload if isinstance(payload, list) else payload.get("events", [])
        except HTTPError as exc:
            headers = exc.headers or {}
            self.usage.update({key: headers.get(header) for key, header in {
                "requests_remaining": "x-requests-remaining",
                "requests_used": "x-requests-used",
                "requests_last": "x-requests-last",
            }.items() if headers.get(header) is not None})
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise LimitsError(f"OddsLink HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError, ValueError) as exc:
            raise LimitsError(f"OddsLink request failed: {exc}") from exc


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
            hashlib.sha256(config.ODDSLINK_API_KEY.encode("utf-8")).hexdigest()
            if config.ODDSLINK_API_KEY else ""
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
                    UNIQUE(observed_at,event_id,bookmaker_key,market_key,outcome_name,point)
                );
                CREATE INDEX IF NOT EXISTS idx_limit_lookup
                ON market_limit_quotes(event_id,bookmaker_key,market_key,outcome_name,point,id);
                CREATE TABLE IF NOT EXISTS market_limit_state (
                    state_key TEXT PRIMARY KEY,
                    state_value TEXT NOT NULL
                );
            """)

    @staticmethod
    def _extract(sport, events, observed_at):
        rows = []
        for event in events:
            for book in event.get("bookmakers", []) or []:
                for market in book.get("markets", []) or []:
                    for outcome in market.get("outcomes", []) or []:
                        size = outcome.get("size") or {}
                        if size.get("kind") != "posted_limit":
                            continue
                        try:
                            amount = float(size["amount"])
                            price = float(outcome["price"]) if outcome.get("price") is not None else None
                        except (KeyError, TypeError, ValueError):
                            continue
                        if amount <= 0:
                            continue
                        rows.append((
                            observed_at, sport, str(event.get("id", "")),
                            str(event.get("commence_time", "")),
                            str(event.get("home_team", "")),
                            str(event.get("away_team", "")),
                            str(book.get("key", "")), str(book.get("title", book.get("key", ""))),
                            str(market.get("key", "")), str(outcome.get("name", "")),
                            outcome.get("point"), price, amount,
                        ))
        return rows

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
        if not config.ODDSLINK_API_KEY:
            self.last_error = "Market limits feed is disabled. Add a free OddsLink API key as ODDSLINK_API_KEY."
            return {"ok": False, "error": self.last_error, "observations": 0}
        now = datetime.now(timezone.utc)
        observed_at = now.isoformat()
        # Persist the last attempt so container restarts cannot reset the
        # free-tier four-hour polling guard. BEGIN IMMEDIATE also coordinates
        # multiple application workers sharing the same SQLite database.
        with self._connect() as state_db:
            state_db.execute("BEGIN IMMEDIATE")
            previous = state_db.execute(
                "SELECT state_value FROM market_limit_state WHERE state_key='last_attempt'"
            ).fetchone()
            previous_key = state_db.execute(
                "SELECT state_value FROM market_limit_state WHERE state_key='last_key_fingerprint'"
            ).fetchone()
            current_key_fingerprint = hashlib.sha256(
                config.ODDSLINK_API_KEY.encode("utf-8")
            ).hexdigest()
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
        client = OddsLinkClient(config.ODDSLINK_API_KEY)
        all_rows, errors = [], []
        diagnostic = {"sports_requested": [], "events_returned": 0, "bookmakers_returned": [], "size_kinds_returned": {}, "posted_limit_outcomes": 0}
        sports = config.LIMITS_SPORTS or config.ODDS_SPORTS
        for configured_sport in sports:
            sport = SPORT_MAP.get(configured_sport)
            if not sport:
                continue
            diagnostic["sports_requested"].append(sport)
            try:
                events = client.fetch(sport)
                diagnostic["events_returned"] += len(events)
                for event in events:
                    for book in event.get("bookmakers", []) or []:
                        key = str(book.get("key", "unknown"))
                        if key not in diagnostic["bookmakers_returned"]:
                            diagnostic["bookmakers_returned"].append(key)
                        for market in book.get("markets", []) or []:
                            for outcome in market.get("outcomes", []) or []:
                                size = outcome.get("size") or {}
                                kind = str(size.get("kind", "no_size"))
                                diagnostic["size_kinds_returned"][kind] = diagnostic["size_kinds_returned"].get(kind, 0) + 1
                                if kind == "posted_limit":
                                    diagnostic["posted_limit_outcomes"] += 1
                all_rows.extend(self._extract(configured_sport, events, observed_at))
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
                      AND outcome_name=? AND point IS ?
                    ORDER BY id DESC LIMIT 1
                """, (row[2], row[6], row[8], row[9], row[10])).fetchone()
                db.execute("""
                    INSERT OR IGNORE INTO market_limit_quotes
                    (observed_at,sport,event_id,commence_time,home_team,away_team,
                     bookmaker_key,bookmaker_title,market_key,outcome_name,point,price,limit_amount)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                (hashlib.sha256(config.ODDSLINK_API_KEY.encode("utf-8")).hexdigest(),),
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
        from urllib.request import Request, urlopen
        for change in changes:
            if change["direction"] != "increased" and not config.LIMITS_ALERT_DECREASES:
                continue
            identity = json.dumps([
                change["event_id"], change["bookmaker_key"], change["market"],
                change["outcome"], change["point"], change["previous_limit"],
                change["limit_amount"], change["observed_at"],
            ], sort_keys=True)
            message = (
                f'**MARKET LIMIT {change["direction"].upper()}** · {change["bookmaker"]} · '
                f'{change["matchup"]} · {change["market"]} {change["outcome"]} '
                f'{change["point"] if change["point"] is not None else ""} · '
                f'{change["previous_limit"]:,.0f} → {change["limit_amount"]:,.0f} '
                f'({change["change_percent"]:+.2f}%) · '
                "Provider-reported posted market limit; currency follows the book."
            )
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
                    data=json.dumps({"content": message[:1900]}).encode(),
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
        current_key_fingerprint = (
            hashlib.sha256(config.ODDSLINK_API_KEY.encode("utf-8")).hexdigest()
            if config.ODDSLINK_API_KEY else ""
        )
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
            "enabled": bool(config.ODDSLINK_API_KEY),
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

    def current(self, sport=None, limit=500):
        sql = """SELECT q.* FROM market_limit_quotes q JOIN (
            SELECT event_id,bookmaker_key,market_key,outcome_name,point,MAX(id) id
            FROM market_limit_quotes GROUP BY event_id,bookmaker_key,market_key,outcome_name,point
        ) x ON x.id=q.id"""
        args = []
        if sport:
            sql += " WHERE q.sport=?"
            args.append(sport)
        sql += " ORDER BY q.commence_time,q.bookmaker_key,q.market_key LIMIT ?"
        args.append(limit)
        with self._connect() as db:
            return [dict(r) for r in db.execute(sql, args).fetchall()]

    def changes(self, sport=None, direction="increased", limit=200):
        """Return limit changes derived from successive persisted provider snapshots."""
        sql = """WITH sequenced AS (
            SELECT q.*,
                   LAG(limit_amount) OVER (
                       PARTITION BY event_id,bookmaker_key,market_key,outcome_name,point
                       ORDER BY id
                   ) AS previous_limit
            FROM market_limit_quotes q
        )
        SELECT * FROM sequenced
        WHERE previous_limit IS NOT NULL AND limit_amount != previous_limit"""
        args = []
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

    def history(self, event_id, bookmaker, market, outcome, point=None, limit=500):
        sql = """SELECT * FROM market_limit_quotes
                 WHERE event_id=? AND bookmaker_key=? AND market_key=? AND outcome_name=?"""
        args = [event_id, bookmaker, market, outcome]
        sql += " AND point IS NULL" if point is None else " AND point=?"
        if point is not None:
            args.append(point)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._connect() as db:
            return [dict(r) for r in db.execute(sql, args).fetchall()]


limits_monitor = MarketLimitsMonitor()
