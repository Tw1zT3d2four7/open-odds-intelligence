import sqlite3
import json
import math
from pathlib import Path
from datetime import datetime, timezone, timedelta
from statistics import median

from .math import american_to_decimal, no_vig_probabilities

from . import config


class OddsDatabase:
    """SQLite store for immutable odds snapshots and quote-level history."""

    def __init__(self, path=None):
        self.path = str(path or config.DATABASE_PATH)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def initialize(self):
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sport TEXT NOT NULL, provider TEXT NOT NULL,
                    fetched_at TEXT NOT NULL, event_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_snapshots_sport_time ON snapshots(sport, fetched_at DESC);
                CREATE TABLE IF NOT EXISTS quotes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
                    event_id TEXT NOT NULL, sport TEXT NOT NULL, sport_title TEXT NOT NULL DEFAULT '',
                    commence_time TEXT NOT NULL DEFAULT '', home_team TEXT NOT NULL DEFAULT '',
                    away_team TEXT NOT NULL DEFAULT '', bookmaker_key TEXT NOT NULL,
                    bookmaker_title TEXT NOT NULL DEFAULT '', market_key TEXT NOT NULL,
                    outcome_name TEXT NOT NULL, outcome_description TEXT NOT NULL DEFAULT '', point REAL, price INTEGER NOT NULL,
                    source_updated_at TEXT,
                    source_provider TEXT NOT NULL DEFAULT '',
                    is_main INTEGER,
                    size_json TEXT,
                    size_source_provider TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_quotes_lookup
                    ON quotes(event_id, market_key, outcome_name, point, bookmaker_key);
                CREATE INDEX IF NOT EXISTS idx_quotes_snapshot ON quotes(snapshot_id);
                CREATE INDEX IF NOT EXISTS idx_quotes_snapshot_event_book_market
                    ON quotes(snapshot_id, event_id, bookmaker_key, market_key, outcome_name);
                CREATE TABLE IF NOT EXISTS sent_alerts (
                    fingerprint TEXT PRIMARY KEY, sent_at TEXT NOT NULL,
                    alert_type TEXT NOT NULL, summary TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS app_state (
                    state_key TEXT PRIMARY KEY,
                    state_value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS clv_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL, sport TEXT NOT NULL DEFAULT '',
                    matchup TEXT NOT NULL DEFAULT '', commence_time TEXT NOT NULL,
                    market_key TEXT NOT NULL, outcome_name TEXT NOT NULL,
                    point REAL, point_key TEXT NOT NULL DEFAULT '',
                    bookmaker_key TEXT NOT NULL, bookmaker_title TEXT NOT NULL DEFAULT '',
                    entry_price INTEGER NOT NULL, entry_fair_probability REAL NOT NULL,
                    entry_ev_percent REAL NOT NULL, entry_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    close_at TEXT, close_fair_probability REAL,
                    close_fair_american INTEGER, closing_books INTEGER,
                    clv_percent REAL,
                    UNIQUE(event_id,market_key,outcome_name,point_key,bookmaker_key)
                );
                CREATE INDEX IF NOT EXISTS idx_clv_pending
                    ON clv_entries(status, commence_time);
                CREATE TABLE IF NOT EXISTS simulation_settlements (
                    event_id TEXT PRIMARY KEY, sport_key TEXT NOT NULL,
                    commence_time TEXT NOT NULL, home_team TEXT NOT NULL, away_team TEXT NOT NULL,
                    home_score INTEGER NOT NULL, away_score INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status = 'final'), source TEXT NOT NULL,
                    source_record_id TEXT NOT NULL, source_updated_at TEXT, imported_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_simulation_settlements_sport_time
                    ON simulation_settlements(sport_key, commence_time);
                CREATE TABLE IF NOT EXISTS simulation_training_settlements (
                    event_id TEXT PRIMARY KEY, sport_key TEXT NOT NULL,
                    commence_time TEXT NOT NULL, home_team TEXT NOT NULL, away_team TEXT NOT NULL,
                    home_score INTEGER NOT NULL, away_score INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status = 'final'), source TEXT NOT NULL,
                    source_record_id TEXT NOT NULL, source_updated_at TEXT, imported_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_simulation_training_sport_time
                    ON simulation_training_settlements(sport_key, commence_time);
            """)
            quote_columns = {row["name"] for row in db.execute("PRAGMA table_info(quotes)")}
            if "source_updated_at" not in quote_columns:
                db.execute("ALTER TABLE quotes ADD COLUMN source_updated_at TEXT")
            if "outcome_description" not in quote_columns:
                db.execute("ALTER TABLE quotes ADD COLUMN outcome_description TEXT NOT NULL DEFAULT ''")
            if "source_provider" not in quote_columns:
                db.execute("ALTER TABLE quotes ADD COLUMN source_provider TEXT NOT NULL DEFAULT ''")
            if "is_main" not in quote_columns:
                db.execute("ALTER TABLE quotes ADD COLUMN is_main INTEGER")
            if "size_json" not in quote_columns:
                db.execute("ALTER TABLE quotes ADD COLUMN size_json TEXT")
            if "size_source_provider" not in quote_columns:
                db.execute("ALTER TABLE quotes ADD COLUMN size_source_provider TEXT NOT NULL DEFAULT ''")
            # Older CLV rows stored the provider's display title instead of
            # the canonical sport key, which made sport-filtered reports miss them.
            # Recover the key from the immutable quote archive where possible.
            db.execute("""
                UPDATE clv_entries
                SET sport = (
                    SELECT q.sport
                    FROM quotes q
                    JOIN snapshots s ON s.id=q.snapshot_id
                    WHERE q.event_id=clv_entries.event_id
                      AND q.commence_time=clv_entries.commence_time
                      AND q.sport <> ''
                    ORDER BY s.id DESC
                    LIMIT 1
                )
                WHERE sport NOT IN (SELECT DISTINCT sport FROM snapshots)
                  AND EXISTS (
                    SELECT 1 FROM quotes q
                    WHERE q.event_id=clv_entries.event_id
                      AND q.commence_time=clv_entries.commence_time
                      AND q.sport <> ''
                  )
            """)



    @staticmethod
    def _normalized_result_team(value):
        return "".join(character.lower() for character in str(value or "") if character.isalnum())

    def save_simulation_settlements(self, results):
        """Persist only final results that exactly match an archived event identity."""
        inserted = corrected = duplicates = 0
        rejected = []
        imported_at = datetime.now(timezone.utc).isoformat()
        for result in results:
            event_id = str(result.get("event_id") or "")
            sport = str(result.get("sport_key") or "")
            if not event_id or not sport or result.get("status") != "final":
                rejected.append({"event_id": event_id, "reason": "missing event identity or non-final status"})
                continue
            try:
                home_score, away_score = result["home_score"], result["away_score"]
                if (isinstance(home_score, bool) or isinstance(away_score, bool)
                        or not isinstance(home_score, int) or not isinstance(away_score, int)
                        or home_score < 0 or away_score < 0):
                    raise ValueError
                result_time = datetime.fromisoformat(str(result["commence_time"]).replace("Z", "+00:00"))
                if result_time.tzinfo is None:
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                rejected.append({"event_id": event_id, "reason": "invalid final score or timezone-aware start time"})
                continue
            with self.connect() as db:
                archived = db.execute(
                    "SELECT q.commence_time,q.home_team,q.away_team FROM quotes q "
                    "JOIN snapshots s ON s.id=q.snapshot_id WHERE q.event_id=? AND q.sport=? "
                    "ORDER BY s.id DESC LIMIT 1",
                    (event_id, sport),
                ).fetchall()
                if not archived:
                    rejected.append({"event_id": event_id, "reason": "event ID is not in the local odds archive"})
                    continue
                quote = archived[0]
                try:
                    archived_time = datetime.fromisoformat(str(quote["commence_time"]).replace("Z", "+00:00"))
                    if archived_time.tzinfo is None:
                        raise ValueError
                except (TypeError, ValueError):
                    rejected.append({"event_id": event_id, "reason": "archived event start time is invalid"})
                    continue
                if abs((archived_time - result_time).total_seconds()) > 300:
                    rejected.append({"event_id": event_id, "reason": "result start time does not match archived event"})
                    continue
                if (self._normalized_result_team(quote["home_team"]) != self._normalized_result_team(result.get("home_team"))
                        or self._normalized_result_team(quote["away_team"]) != self._normalized_result_team(result.get("away_team"))):
                    rejected.append({"event_id": event_id, "reason": "result home/away teams do not match archived event"})
                    continue
                source_updated_at = result.get("source_updated_at")
                incoming_updated = None
                if source_updated_at:
                    try:
                        incoming_updated = datetime.fromisoformat(str(source_updated_at).replace("Z", "+00:00"))
                        if incoming_updated.tzinfo is None:
                            raise ValueError
                    except (TypeError, ValueError):
                        rejected.append({"event_id": event_id, "reason": "source update timestamp must be ISO-8601 with timezone"})
                        continue
                existing = db.execute(
                    "SELECT home_score,away_score,source_updated_at FROM simulation_settlements WHERE event_id=?",
                    (event_id,),
                ).fetchone()
                if existing is None:
                    db.execute(
                        """INSERT INTO simulation_settlements(
                           event_id,sport_key,commence_time,home_team,away_team,home_score,away_score,
                           status,source,source_record_id,source_updated_at,imported_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (event_id, sport, result["commence_time"], result["home_team"], result["away_team"],
                         home_score, away_score, "final", str(result.get("source") or "unknown"),
                         str(result.get("source_record_id") or event_id), source_updated_at, imported_at),
                    )
                    inserted += 1
                    continue
                previous_updated = None
                if existing["source_updated_at"]:
                    try:
                        previous_updated = datetime.fromisoformat(str(existing["source_updated_at"]).replace("Z", "+00:00"))
                        if previous_updated.tzinfo is None:
                            raise ValueError
                    except (TypeError, ValueError):
                        rejected.append({"event_id": event_id, "reason": "stored source update timestamp is invalid; correction not applied"})
                        continue
                # A correction is accepted only with a strictly newer, timezone-aware
                # provider timestamp. Missing timestamps can never overwrite a result.
                if incoming_updated is not None and (previous_updated is None or incoming_updated > previous_updated):
                    db.execute(
                        """UPDATE simulation_settlements SET home_score=?,away_score=?,source=?,
                           source_record_id=?,source_updated_at=?,imported_at=?
                           WHERE event_id=?""",
                        (home_score, away_score, str(result.get("source") or "unknown"),
                         str(result.get("source_record_id") or event_id), str(source_updated_at), imported_at, event_id),
                    )
                    corrected += 1
                else:
                    duplicates += 1
        return {"inserted": inserted, "corrected": corrected, "duplicates": duplicates, "rejected": rejected}

    def save_simulation_training_settlements(self, results):
        """Persist operator-verified historical outcomes used only for model training.

        Unlike live settlements, these rows need not have archived odds. They
        remain in a separate table and are marked training_only when read back,
        so they cannot silently become calibration or holdout observations.
        """
        from .simulation import _family

        inserted = duplicates = 0
        rejected = []
        imported_at = datetime.now(timezone.utc).isoformat()
        for result in results:
            event_id = str(result.get("event_id") or "").strip()
            sport = str(result.get("sport_key") or "").strip()
            if not event_id or not sport or result.get("status") != "final":
                rejected.append({"event_id": event_id, "reason": "missing event identity or non-final status"})
                continue
            if _family(sport) is None:
                rejected.append({"event_id": event_id, "reason": "unsupported simulator league"})
                continue
            source = str(result.get("source") or "").strip()
            home_team = str(result.get("home_team") or "").strip()
            away_team = str(result.get("away_team") or "").strip()
            try:
                home_score, away_score = result["home_score"], result["away_score"]
                commence = datetime.fromisoformat(str(result["commence_time"]).replace("Z", "+00:00"))
                if (isinstance(home_score, bool) or isinstance(away_score, bool)
                        or not isinstance(home_score, int) or not isinstance(away_score, int)
                        or home_score < 0 or away_score < 0 or commence.tzinfo is None
                        or not home_team or not away_team
                        or self._normalized_result_team(home_team) == self._normalized_result_team(away_team)
                        or not source):
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                rejected.append({"event_id": event_id, "reason": "invalid final score, teams, source, or timezone-aware start time"})
                continue
            with self.connect() as db:
                archived = db.execute(
                    "SELECT 1 FROM simulation_settlements WHERE event_id=?", (event_id,)
                ).fetchone()
                existing = db.execute(
                    "SELECT 1 FROM simulation_training_settlements WHERE event_id=?", (event_id,)
                ).fetchone()
                if archived or existing:
                    duplicates += 1
                    continue
                db.execute(
                    """INSERT INTO simulation_training_settlements(
                       event_id,sport_key,commence_time,home_team,away_team,home_score,away_score,
                       status,source,source_record_id,source_updated_at,imported_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (event_id, sport, result["commence_time"], home_team, away_team,
                     home_score, away_score, "final", source,
                     str(result.get("source_record_id") or event_id), None, imported_at),
                )
                inserted += 1
        return {"inserted": inserted, "corrected": 0, "duplicates": duplicates, "rejected": rejected}

    def simulation_settlements(self, sport=None):
        sql = """SELECT * FROM (
            SELECT a.*, 0 AS training_only FROM simulation_settlements a
            UNION ALL
            SELECT t.*, 1 AS training_only FROM simulation_training_settlements t
            WHERE NOT EXISTS (
                SELECT 1 FROM simulation_settlements a WHERE a.event_id=t.event_id
            )
        )"""
        args = []
        if sport:
            sql += " WHERE sport_key=?"
            args.append(sport)
        sql += " ORDER BY commence_time,event_id"
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql, args).fetchall()]

    def pending_simulation_result_dates(self, sports, lookback_days=7, now=None, offset_minutes=0, recheck_days=0):
        """Return unscored dates and, optionally, dates with recently settled events."""
        if not sports:
            return []
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        since = now.astimezone(timezone.utc) - timedelta(days=max(1, int(lookback_days)))
        placeholders = ",".join("?" for _ in sports)
        sql = (
            "SELECT DISTINCT q.event_id,q.sport,q.commence_time FROM quotes q "
            "WHERE q.sport IN (" + placeholders + ") "
            "AND substr(q.commence_time,1,10)>=? AND substr(q.commence_time,1,10)<=? "
            "AND (NOT EXISTS (SELECT 1 FROM simulation_settlements r WHERE r.event_id=q.event_id) "
            "OR (? > 0 AND EXISTS (SELECT 1 FROM simulation_settlements r WHERE r.event_id=q.event_id "
            "AND substr(r.commence_time,1,10)>=?)))"
        )
        recheck_days = max(0, min(7, int(recheck_days)))
        recheck_since = (now.astimezone(timezone.utc) - timedelta(days=recheck_days)).date().isoformat()
        with self.connect() as db:
            rows = db.execute(sql, [*sports, since.date().isoformat(), now.date().isoformat(),
                                    recheck_days, recheck_since]).fetchall()
        grouped = {}
        for row in rows:
            try:
                commence = datetime.fromisoformat(str(row["commence_time"]).replace("Z", "+00:00"))
                if commence.tzinfo is None or commence.astimezone(timezone.utc) >= now.astimezone(timezone.utc):
                    continue
            except (TypeError, ValueError):
                continue
            event_date = (commence.astimezone(timezone.utc) + timedelta(minutes=int(offset_minutes))).date().isoformat()
            key = (row["sport"], event_date)
            grouped.setdefault(key, set()).add(row["event_id"])
        return [{"sport": sport, "event_date": event_date, "pending_events": len(event_ids)}
                for (sport, event_date), event_ids in sorted(grouped.items(), key=lambda item: (item[0][1], item[0][0]))]

    def get_state(self, key):
        with self.connect() as db:
            row = db.execute(
                "SELECT state_value FROM app_state WHERE state_key=?", (key,)
            ).fetchone()
        return row["state_value"] if row else None

    def set_state(self, key, value):
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO app_state(state_key,state_value) VALUES(?,?)",
                (key, str(value)),
            )

    def save_snapshot(self, sport, events, provider="unknown", fetched_at=None):
        fetched_at = fetched_at or datetime.now(timezone.utc).isoformat()
        if not isinstance(events, list):
            events = []
        # Event IDs are the primary identity used by every downstream quote,
        # movement, CLV, and simulator lookup. Never persist anonymous events:
        # they would all collapse into one empty-ID matchup in latest_events().
        def stable_event_id(event):
            if not isinstance(event, dict):
                return None
            value = event.get("id")
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                return None
            value = str(value).strip()
            return value or None

        valid_events = [
            event for event in events if stable_event_id(event) is not None
        ]
        unique_event_ids = {stable_event_id(event) for event in valid_events}
        with self.connect() as db:
            cur = db.execute(
                "INSERT INTO snapshots(sport,provider,fetched_at,event_count) VALUES(?,?,?,?)",
                (sport, provider, fetched_at, len(unique_event_ids)))
            snapshot_id = cur.lastrowid
            rows_by_identity = {}
            conflicted_quotes = set()
            event_metadata_by_id = {}
            conflicted_events = set()
            for event in valid_events:
                event_id = stable_event_id(event)
                metadata = (
                    event.get("sport_title", ""), event.get("commence_time", ""),
                    event.get("home_team", ""), event.get("away_team", ""),
                )
                previous_metadata = event_metadata_by_id.get(event_id)
                if previous_metadata is not None and previous_metadata != metadata:
                    conflicted_events.add(event_id)
                else:
                    event_metadata_by_id[event_id] = metadata
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
                            # Provider odds are canonical integer American prices.
                            # Do not coerce booleans, fractional values, or numeric strings:
                            # coercion can silently turn malformed feed data into real quotes.
                            price = outcome.get("price")
                            if (isinstance(price, bool) or not isinstance(price, int)
                                    or price == 0 or -100 < price < 100):
                                continue
                            market_key = market.get("key")
                            book_key = book.get("key")
                            outcome_name = outcome.get("name")
                            if not all(isinstance(value, str) and value.strip()
                                       for value in (market_key, book_key, outcome_name)):
                                continue
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
                            description = outcome.get("description") or outcome.get("participant") or ""
                            if not isinstance(description, str):
                                continue
                            updated_at = (
                                outcome.get("source_updated_at") or outcome.get("updated_at") or
                                market.get("last_update") or book.get("last_update")
                            )
                            if updated_at is not None and not isinstance(updated_at, str):
                                continue
                            event_metadata = (
                                event.get("sport_title", ""), event.get("commence_time", ""),
                                event.get("home_team", ""), event.get("away_team", ""),
                            )
                            if not all(isinstance(value, str) for value in event_metadata):
                                continue
                            book_title = book.get("title")
                            if not isinstance(book_title, str) or not book_title.strip():
                                book_title = book_key
                            row = (snapshot_id, event_id, sport,
                                   *event_metadata, book_key, book_title, market_key,
                                   outcome_name, description, point, price, updated_at,
                                   str(outcome.get("source_provider") or ""),
                                   (1 if outcome.get("main") else 0) if "main" in outcome else None,
                                   json.dumps(outcome.get("size"), sort_keys=True)
                                   if isinstance(outcome.get("size"), dict) else None,
                                   str(outcome.get("size_source_provider") or ""))
                            identity = (row[1], book_key, market_key, outcome_name, description, point)
                            if identity in conflicted_quotes:
                                continue
                            previous = rows_by_identity.get(identity)
                            if previous is None:
                                rows_by_identity[identity] = row
                            elif previous[13] != price or previous[3:7] != row[3:7]:
                                # Two different values for the same exact selection in
                                # one provider snapshot are ambiguous. Drop the selection
                                # rather than allowing duplicate outcomes to corrupt
                                # consensus, arbitrage, or simulator readiness.
                                rows_by_identity.pop(identity, None)
                                conflicted_quotes.add(identity)
            rows = [
                row for row in rows_by_identity.values()
                if row[1] not in conflicted_events
            ]
            db.executemany("""INSERT INTO quotes(snapshot_id,event_id,sport,sport_title,commence_time,
                home_team,away_team,bookmaker_key,bookmaker_title,market_key,outcome_name,outcome_description,point,price,
                source_updated_at,source_provider,is_main,size_json,size_source_provider)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
        return snapshot_id

    def latest_events(self, sport=None, limit=None, offset=0):
        """Load the latest stored event snapshots with linear-time grouping.

        Keep lookup maps while assembling each event. Rebuilding bookmaker and
        market dictionaries for every quote made this hot public read quadratic
        for large multi-book snapshots.
        """
        with self.connect() as db:
            if sport:
                snapshots = db.execute(
                    "SELECT id,sport,fetched_at FROM snapshots WHERE sport=? ORDER BY id DESC LIMIT 1",
                    (sport,)).fetchall()
            else:
                snapshots = db.execute("""SELECT s.id,s.sport,s.fetched_at FROM snapshots s
                    JOIN (SELECT sport,MAX(id) id FROM snapshots GROUP BY sport) x ON x.id=s.id
                    ORDER BY s.sport""").fetchall()
            events = []
            for snap in snapshots:
                page_clause = ""
                page_args = [snap["id"]]
                if limit is not None:
                    page_clause = """ AND event_id IN (
                        SELECT event_id FROM quotes WHERE snapshot_id=?
                        GROUP BY event_id ORDER BY MIN(commence_time),event_id
                        LIMIT ? OFFSET ?
                    )"""
                    page_args.extend([snap["id"], max(1, min(int(limit), 100)), max(0, int(offset))])
                rows = db.execute(
                    """SELECT event_id,sport_title,commence_time,home_team,away_team,
                              bookmaker_key,bookmaker_title,market_key,outcome_name,
                              outcome_description,price,source_updated_at,source_provider,
                              is_main,size_json,size_source_provider,point
                       FROM quotes WHERE snapshot_id=?""" + page_clause + """
                       ORDER BY commence_time,event_id,bookmaker_key,market_key,outcome_name""",
                    page_args,
                ).fetchall()
                grouped = {}
                bookmaker_maps = {}
                market_maps = {}
                for row in rows:
                    event_id = row["event_id"]
                    event = grouped.get(event_id)
                    if event is None:
                        event = {
                            "id": event_id, "sport_key": snap["sport"],
                            "sport_title": row["sport_title"],
                            "commence_time": row["commence_time"],
                            "home_team": row["home_team"], "away_team": row["away_team"],
                            "source": "stored", "retrieved_at": snap["fetched_at"],
                            "bookmakers": [],
                        }
                        grouped[event_id] = event
                        bookmaker_maps[event_id] = {}
                        market_maps[event_id] = {}
                    books = bookmaker_maps[event_id]
                    book = books.get(row["bookmaker_key"])
                    if book is None:
                        book = {"key": row["bookmaker_key"], "title": row["bookmaker_title"], "markets": []}
                        books[row["bookmaker_key"]] = book
                        event["bookmakers"].append(book)
                    book_id = (event_id, row["bookmaker_key"])
                    markets = market_maps.get(book_id)
                    if markets is None:
                        markets = {}
                        market_maps[book_id] = markets
                    market = markets.get(row["market_key"])
                    if market is None:
                        market = {"key": row["market_key"], "outcomes": []}
                        markets[row["market_key"]] = market
                        book["markets"].append(market)
                    outcome = {"name": row["outcome_name"], "price": row["price"]}
                    if row["outcome_description"]:
                        outcome["description"] = row["outcome_description"]
                    if row["source_updated_at"]:
                        outcome["updated_at"] = row["source_updated_at"]
                    if row["source_provider"]:
                        outcome["source_provider"] = row["source_provider"]
                    if row["is_main"] is not None:
                        outcome["main"] = bool(row["is_main"])
                    if row["size_json"]:
                        try:
                            outcome["size"] = json.loads(row["size_json"])
                        except (TypeError, ValueError):
                            pass
                    if row["size_source_provider"]:
                        outcome["size_source_provider"] = row["size_source_provider"]
                    if row["point"] is not None:
                        outcome["point"] = row["point"]
                    market["outcomes"].append(outcome)
                events.extend(grouped.values())
        return events

    def history(self, event_id, market_key, outcome_name, bookmaker_key=None, point=None,
                limit=100, outcome_description=None):
        sql = """SELECT s.fetched_at,q.bookmaker_key,q.bookmaker_title,q.market_key,
                 q.outcome_name,q.outcome_description,q.point,q.price,q.source_updated_at FROM quotes q
                 JOIN snapshots s ON s.id=q.snapshot_id
                 WHERE q.event_id=? AND q.market_key=? AND q.outcome_name=?"""
        args = [event_id, market_key, outcome_name]
        if bookmaker_key:
            sql += " AND q.bookmaker_key=?"
            args.append(bookmaker_key)
        if outcome_description is not None:
            sql += " AND q.outcome_description=?"
            args.append(outcome_description)
        sql += " AND q.point IS NULL" if point is None else " AND q.point=?"
        if point is not None:
            args.append(point)
        sql += " ORDER BY s.fetched_at DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql, args).fetchall()]


    def movement(self, sport=None, limit=200):
        """Return one opening-to-latest movement summary per sportsbook selection.

        When a book offers a single line for a selection, line transitions are
        followed across snapshots. When alternate lines coexist, each exact
        point is tracked independently so one ladder rung is never mistaken
        for another. The full quote timeline remains available via history().
        """
        sql = """
            SELECT s.id AS snapshot_id, s.fetched_at, q.source_updated_at,
                   q.event_id, q.sport,
                   q.sport_title, q.commence_time, q.home_team, q.away_team,
                   q.bookmaker_key, q.bookmaker_title, q.market_key,
                   q.outcome_name, q.outcome_description, q.point, q.price
            FROM quotes q JOIN snapshots s ON s.id=q.snapshot_id
            WHERE (? IS NULL OR q.sport=?)
            ORDER BY s.id ASC, q.event_id, q.bookmaker_key,
                     q.market_key, q.outcome_name, q.point
        """
        with self.connect() as db:
            rows = [dict(row) for row in db.execute(sql, (sport, sport)).fetchall()]

        grouped = {}
        for row in rows:
            key = (row["event_id"], row["bookmaker_key"],
                   row["market_key"], row["outcome_name"], row.get("outcome_description") or "")
            grouped.setdefault(key, {}).setdefault(row["snapshot_id"], {})[row["point"]] = row

        def implied(price):
            return 100 * (100 / (price + 100) if price > 0
                          else abs(price) / (abs(price) + 100))

        results = []
        for snapshots in grouped.values():
            ordered = [snapshots[snapshot_id]
                       for snapshot_id in sorted(snapshots)]
            if len(ordered) < 2:
                continue

            # A single offered line in every observation is a genuine line
            # progression. If any snapshot contains a ladder, compare exact
            # points only and never infer movement between alternate lines.
            if all(len(snapshot) == 1 for snapshot in ordered):
                timelines = [[next(iter(snapshot.values())) for snapshot in ordered]]
            else:
                by_point = {}
                for snapshot in ordered:
                    for point, row in snapshot.items():
                        by_point.setdefault(point, []).append(row)
                timelines = [timeline for timeline in by_point.values()
                             if len(timeline) >= 2]

            for timeline in timelines:
                opening, previous, current = timeline[0], timeline[-2], timeline[-1]
                price_changes = sum(
                    left["price"] != right["price"]
                    for left, right in zip(timeline, timeline[1:])
                )
                line_changes = sum(
                    left["point"] != right["point"]
                    for left, right in zip(timeline, timeline[1:])
                )
                if not price_changes and not line_changes:
                    continue

                opening_probability = implied(opening["price"])
                previous_probability = implied(previous["price"])
                current_probability = implied(current["price"])
                probability_delta = round(current_probability - previous_probability, 3)
                open_delta = round(current_probability - opening_probability, 3)
                line_changed = opening["point"] != current["point"]
                price_changed = opening["price"] != current["price"]
                if line_changed and price_changed:
                    change_type = "line_and_price"
                elif line_changed:
                    change_type = "line"
                else:
                    change_type = "price"

                results.append({
                    "event_id": current["event_id"], "sport": current["sport"],
                    "matchup": f'{current["away_team"]} @ {current["home_team"]}',
                    "commence_time": current["commence_time"], "market": current["market_key"],
                    "outcome": current["outcome_name"], "outcome_description": current.get("outcome_description") or "",
                    "bookmaker": current["bookmaker_title"],
                    "bookmaker_key": current["bookmaker_key"],
                    "opening_point": opening["point"], "previous_point": previous["point"],
                    "point": current["point"], "opening_price": opening["price"],
                    "previous_price": previous["price"], "price": current["price"],
                    "price_change": current["price"] - previous["price"],
                    "price_change_from_open": current["price"] - opening["price"],
                    "implied_probability_change": probability_delta,
                    "implied_probability_change_from_open": open_delta,
                    "previous_at": previous["fetched_at"], "updated_at": current["fetched_at"],
                    "source_updated_at": current.get("source_updated_at"),
                    "opening_source_updated_at": opening.get("source_updated_at"),
                    "timestamp_basis": "sportsbook" if current.get("source_updated_at") else "retrieved",
                    "opening_at": opening["fetched_at"], "first_seen": opening["fetched_at"],
                    "observations": len(timeline), "price_changes": price_changes,
                    "line_changes": line_changes, "move_count": price_changes + line_changes,
                    "change_type": change_type,
                    "direction": "shortening" if open_delta > 0 else "drifting" if open_delta < 0 else "line_only",
                })

        results.sort(key=lambda row: (
            abs(row["implied_probability_change_from_open"]),
            row["move_count"], row["updated_at"]
        ), reverse=True)
        return results[:max(1, min(int(limit), 1000))]


    def movement_history(self, event_id, market_key, outcome_name,
                         bookmaker_key, limit=500, outcome_description=None):
        """Return a chronological quote timeline, including every observed line."""
        with self.connect() as db:
            rows = db.execute(
                """SELECT s.fetched_at, q.source_updated_at, q.point, q.price
                   FROM quotes q JOIN snapshots s ON s.id=q.snapshot_id
                   WHERE q.event_id=? AND q.market_key=? AND q.outcome_name=?
                     AND q.bookmaker_key=? AND (? IS NULL OR q.outcome_description=?)
                   ORDER BY s.id DESC LIMIT ?""",
                (event_id, market_key, outcome_name, bookmaker_key,
                 outcome_description, outcome_description, max(1, min(int(limit), 1000))),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]


    def record_clv_candidates(self, candidates, observed_at=None):
        """Persist the first valid EV observation for each exact selection."""
        if not isinstance(candidates, list):
            return 0
        observed_at = observed_at or datetime.now(timezone.utc).isoformat()
        observed_dt = self._as_utc(observed_at)
        if observed_dt is None:
            return 0
        inserted = 0
        with self.connect() as db:
            for row in candidates:
                if not isinstance(row, dict):
                    continue
                event_value = row.get("event_id")
                if isinstance(event_value, bool) or not isinstance(event_value, (str, int)):
                    continue
                event_id = str(event_value).strip()
                if not event_id:
                    continue

                price = row.get("price")
                if (isinstance(price, bool) or not isinstance(price, int)
                        or price == 0 or -100 < price < 100):
                    continue
                market = row.get("market")
                outcome = row.get("outcome")
                bookmaker_key = row.get("bookmaker_key")
                if not all(isinstance(value, str) and value.strip()
                           for value in (market, outcome, bookmaker_key)):
                    continue
                market, outcome, bookmaker_key = (
                    market.strip(), outcome.strip(), bookmaker_key.strip()
                )
                if market.startswith("player_"):
                    continue

                commence = row.get("commence_time")
                start = self._as_utc(commence)
                if start is None or observed_dt >= start:
                    continue

                raw_probability = row.get("fair_probability")
                raw_ev = row.get("ev_percent")
                if isinstance(raw_probability, bool) or isinstance(raw_ev, bool):
                    continue
                try:
                    fair_probability = float(raw_probability)
                    ev_percent = float(raw_ev)
                except (TypeError, ValueError, OverflowError):
                    continue
                if (not math.isfinite(fair_probability) or not 0 < fair_probability < 1
                        or not math.isfinite(ev_percent)):
                    continue

                point = row.get("point")
                if point is None:
                    point_key = ""
                else:
                    if isinstance(point, bool) or not isinstance(point, (int, float)):
                        continue
                    try:
                        point = float(point)
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if not math.isfinite(point):
                        continue
                    point_key = str(abs(point) if market == "spreads" else point)

                sport = row.get("sport_key") or row.get("sport") or ""
                matchup = row.get("matchup") or ""
                title = row.get("bookmaker") or bookmaker_key
                if not all(isinstance(value, str) for value in (sport, matchup, title)):
                    continue

                if market == "spreads" and point is not None:
                    legacy_or_current = db.execute(
                        """SELECT 1 FROM clv_entries
                           WHERE event_id=? AND market_key='spreads'
                             AND outcome_name=? AND bookmaker_key=?
                             AND point IS NOT NULL AND ABS(point)=?
                           LIMIT 1""",
                        (event_id, outcome, bookmaker_key, abs(point)),
                    ).fetchone()
                    if legacy_or_current:
                        continue
                cur = db.execute(
                    """INSERT OR IGNORE INTO clv_entries(
                       event_id,sport,matchup,commence_time,market_key,outcome_name,
                       point,point_key,bookmaker_key,bookmaker_title,entry_price,
                       entry_fair_probability,entry_ev_percent,entry_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (event_id, sport, matchup, str(commence), market, outcome,
                     point, point_key, bookmaker_key, title, price,
                     fair_probability, ev_percent, str(observed_at)))
                inserted += max(0, cur.rowcount)
        return inserted

    @staticmethod
    def _as_utc(value):
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
        except (TypeError, ValueError):
            return None

    def _closing_consensus(self, db, entry):
        start = self._as_utc(entry["commence_time"])
        entry_at = self._as_utc(entry["entry_at"])
        if start is None or entry_at is None:
            return None
        snapshots = db.execute(
            """SELECT DISTINCT s.id,s.fetched_at FROM snapshots s
               JOIN quotes q ON q.snapshot_id=s.id
               WHERE q.event_id=? ORDER BY s.id DESC""",
            (entry["event_id"],)).fetchall()
        earliest = datetime.min.replace(tzinfo=timezone.utc)
        snapshots = sorted(
            snapshots,
            key=lambda snapshot: self._as_utc(snapshot["fetched_at"]) or earliest,
            reverse=True,
        )
        for snapshot in snapshots:
            fetched = self._as_utc(snapshot["fetched_at"])
            # A closing observation must be strictly later than the
            # captured entry snapshot. The refresh that created the entry
            # cannot also serve as its close.
            if (fetched is None or fetched <= entry_at or fetched >= start or
                start - fetched > timedelta(minutes=config.CLV_MAX_CLOSE_AGE_MINUTES)):
                continue
            quotes = db.execute(
                """SELECT bookmaker_key,market_key,outcome_name,point,price
                   FROM quotes WHERE snapshot_id=? AND event_id=? AND market_key=?""",
                (snapshot["id"], entry["event_id"], entry["market_key"])).fetchall()
            by_book_line = {}
            target_line = None if entry["point"] is None else abs(float(entry["point"])) if entry["market_key"] == "spreads" else float(entry["point"])
            for quote in quotes:
                if quote["market_key"] == "h2h":
                    line = None
                elif quote["point"] is None:
                    line = None
                elif quote["market_key"] == "spreads":
                    line = abs(float(quote["point"]))
                else:
                    line = float(quote["point"])
                if line != target_line:
                    continue
                key = quote["bookmaker_key"]
                by_book_line.setdefault(key, []).append(dict(quote))
            valid_books = {}
            # Main h2h markets are two-way for most supported sports, but
            # soccer h2h is a three-way result market (home/draw/away).
            # Requiring the expected cardinality prevents a feed that omits
            # the draw at every peer book from being mislabeled "complete".
            expected_outcomes = (
                3 if entry["market_key"] == "h2h"
                and str(entry["sport"]).startswith("soccer_") else 2
            )
            for book, outcomes in by_book_line.items():
                if book == entry["bookmaker_key"] or len(outcomes) != expected_outcomes:
                    continue
                # Spread feeds can encode the same side with opposite
                # point signs. Normalize only the line magnitude for market
                # completeness; keep outcome_name intact so Home and Away
                # can never be swapped or treated as the same selection.
                identities = [
                    (o["outcome_name"],
                     abs(float(o["point"])) if entry["market_key"] == "spreads" and o["point"] is not None
                     else o["point"])
                    for o in outcomes
                ]
                if (len(set(identities)) != len(identities) or any(
                    isinstance(o["price"], bool) or not isinstance(o["price"], int)
                    or o["price"] == 0 or -100 < o["price"] < 100
                    for o in outcomes
                )):
                    continue
                valid_books[book] = (tuple(sorted(identities, key=lambda item: (item[0], str(item[1])))), outcomes)
            # Only compare complete peer markets with identical outcome/line sets.
            identity_counts = {}
            for identities, outcomes in valid_books.values():
                identity_counts[identities] = identity_counts.get(identities, 0) + 1
            peer_probabilities = []
            if identity_counts:
                highest_count = max(identity_counts.values())
                common_sets = [
                    identities for identities, count in identity_counts.items()
                    if count == highest_count
                ]
                # If equally common books disagree about the outcome set,
                # do not choose an arbitrary market definition for CLV.
                if highest_count < 2 or len(common_sets) != 1:
                    continue
                common = common_sets[0]
                for identities, outcomes in valid_books.values():
                    if identities != common:
                        continue
                    try:
                        probs = no_vig_probabilities([o["price"] for o in outcomes])
                    except ValueError:
                        continue
                    for outcome, probability in zip(outcomes, probs):
                        same_point = outcome["point"] == entry["point"]
                        if entry["market_key"] == "spreads":
                            same_point = (
                                outcome["point"] is not None and entry["point"] is not None
                                and abs(float(outcome["point"])) == abs(float(entry["point"]))
                            )
                        if outcome["outcome_name"] == entry["outcome_name"] and same_point:
                            peer_probabilities.append(probability)
                            break
            if len(peer_probabilities) >= 2:
                fair = float(median(peer_probabilities))
                if 0 < fair < 1:
                    close_decimal = 1 / fair
                    entry_price = entry["entry_price"]
                    if (isinstance(entry_price, bool) or not isinstance(entry_price, int)
                            or entry_price == 0 or -100 < entry_price < 100):
                        return None
                    entry_decimal = american_to_decimal(entry_price)
                    clv = (entry_decimal / close_decimal - 1) * 100
                    if not math.isfinite(clv):
                        return None
                    close_american = (round((close_decimal - 1) * 100) if close_decimal >= 2
                                      else round(-100 / (close_decimal - 1)))
                    return {"close_at": snapshot["fetched_at"],
                            "close_fair_probability": fair,
                            "close_fair_american": close_american,
                            "closing_books": len(peer_probabilities),
                            "clv_percent": clv}
        return None

    def clv_report(self, sport=None, limit=500):
        """Resolve tracked entries against the last pregame no-vig peer consensus.

        Positive CLV means the captured entry price was longer than the fair
        closing consensus price. Only complete same-line markets from at least
        two other books are eligible; no post-start quotes are considered.
        """
        now = datetime.now(timezone.utc)
        with self.connect() as db:
            pending_sql = "SELECT * FROM clv_entries WHERE status='pending'"
            pending_args = []
            if sport:
                pending_sql += " AND sport=?"
                pending_args.append(sport)
            pending = db.execute(pending_sql, pending_args).fetchall()
            for entry in pending:
                start = self._as_utc(entry["commence_time"])
                if start is None or now < start:
                    continue
                close = self._closing_consensus(db, entry)
                if close:
                    db.execute(
                        """UPDATE clv_entries SET status='closed',close_at=?,
                           close_fair_probability=?,close_fair_american=?,
                           closing_books=?,clv_percent=? WHERE id=? AND status='pending'""",
                        (close["close_at"], close["close_fair_probability"],
                         close["close_fair_american"], close["closing_books"],
                         close["clv_percent"], entry["id"]))
                else:
                    # A refresh can run immediately after kickoff while the
                    # final pregame snapshot has not yet been collected. Keep
                    # retrying for a bounded grace period, but never use live
                    # or post-start quotes as a substitute for a closing line.
                    grace_minutes = max(0, int(config.CLV_CLOSE_GRACE_MINUTES))
                    if now >= start + timedelta(minutes=grace_minutes):
                        db.execute(
                            "UPDATE clv_entries SET status='insufficient_close_data' WHERE id=?",
                            (entry["id"],),
                        )
            sql = "SELECT * FROM clv_entries"
            args = []
            if sport:
                sql += " WHERE sport=?"
                args.append(sport)
            sql += " ORDER BY entry_at DESC LIMIT ?"
            args.append(max(1, min(int(limit), 2000)))
            rows = [dict(row) for row in db.execute(sql, args).fetchall()]
            summary_sql = """SELECT COUNT(*) total,
                SUM(CASE WHEN status='closed' THEN 1 ELSE 0 END) closed,
                SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) pending,
                AVG(CASE WHEN status='closed' THEN clv_percent END) average_clv_percent,
                SUM(CASE WHEN status='closed' AND clv_percent>0 THEN 1 ELSE 0 END) positive_clv,
                SUM(CASE WHEN status='closed' AND clv_percent<0 THEN 1 ELSE 0 END) negative_clv
                FROM clv_entries"""
            if sport:
                summary_sql += " WHERE sport=?"
            summary = db.execute(summary_sql, [sport] if sport else []).fetchone()
        return {"entries": rows, "summary": dict(summary),
                "method": "Entry American odds compared with last pregame no-vig consensus price from at least two other complete same-line sportsbooks. Positive means the entry price beat that consensus close; this is not betting profit or a win-rate measure."}

    def claim_alert(self, fingerprint, alert_type, summary, sent_at=None):
        """Atomically claim an alert fingerprint; returns False if already sent."""
        sent_at = sent_at or datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            cur = db.execute(
                "INSERT OR IGNORE INTO sent_alerts(fingerprint,sent_at,alert_type,summary) VALUES(?,?,?,?)",
                (fingerprint, sent_at, alert_type, summary))
            return cur.rowcount == 1

    def release_alert(self, fingerprint):
        with self.connect() as db:
            db.execute("DELETE FROM sent_alerts WHERE fingerprint=?", (fingerprint,))

    def alert_status(self):
        with self.connect() as db:
            row = db.execute("SELECT COUNT(*) AS sent, MAX(sent_at) AS last_sent FROM sent_alerts").fetchone()
            return dict(row)

    def snapshot_status(self):
        with self.connect() as db:
            row = db.execute("""SELECT COUNT(*) snapshots,COALESCE(SUM(event_count),0) events,
                                MAX(fetched_at) last_updated FROM snapshots""").fetchone()
            latest = db.execute(
                """SELECT s.sport,s.fetched_at,s.event_count
                   FROM snapshots s
                   JOIN (SELECT sport,MAX(id) AS id FROM snapshots GROUP BY sport) x
                     ON x.id=s.id
                   ORDER BY s.sport"""
            ).fetchall()
            result = dict(row)
            result["sports"] = [dict(item) for item in latest]
            return result
