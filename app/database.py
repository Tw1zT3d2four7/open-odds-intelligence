import sqlite3
from pathlib import Path
from datetime import datetime, timezone

from . import config


class OddsDatabase:
    """SQLite store for immutable odds snapshots and quote-level history."""

    def __init__(self, path=None):
        self.path = str(path or config.DATABASE_PATH)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def initialize(self):
        with self.connect() as db:
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
                    outcome_name TEXT NOT NULL, point REAL, price INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_quotes_lookup
                    ON quotes(event_id, market_key, outcome_name, point, bookmaker_key);
                CREATE INDEX IF NOT EXISTS idx_quotes_snapshot ON quotes(snapshot_id);
                CREATE TABLE IF NOT EXISTS sent_alerts (
                    fingerprint TEXT PRIMARY KEY, sent_at TEXT NOT NULL,
                    alert_type TEXT NOT NULL, summary TEXT NOT NULL
                );
            """)

    def save_snapshot(self, sport, events, provider="unknown", fetched_at=None):
        fetched_at = fetched_at or datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            cur = db.execute(
                "INSERT INTO snapshots(sport,provider,fetched_at,event_count) VALUES(?,?,?,?)",
                (sport, provider, fetched_at, len(events)))
            snapshot_id = cur.lastrowid
            rows = []
            for event in events:
                for book in event.get("bookmakers", []):
                    for market in book.get("markets", []):
                        for outcome in market.get("outcomes", []):
                            try:
                                price = int(outcome["price"])
                            except (KeyError, TypeError, ValueError):
                                continue
                            if not outcome.get("name") or not market.get("key") or not book.get("key"):
                                continue
                            rows.append((snapshot_id, str(event.get("id", "")), sport,
                                event.get("sport_title", ""), event.get("commence_time", ""),
                                event.get("home_team", ""), event.get("away_team", ""),
                                book["key"], book.get("title", book["key"]), market["key"],
                                outcome["name"], outcome.get("point"), price))
            db.executemany("""INSERT INTO quotes(snapshot_id,event_id,sport,sport_title,commence_time,
                home_team,away_team,bookmaker_key,bookmaker_title,market_key,outcome_name,point,price)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
        return snapshot_id

    def latest_events(self, sport=None):
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
                rows = db.execute("SELECT * FROM quotes WHERE snapshot_id=? ORDER BY event_id,bookmaker_key,market_key,outcome_name",
                                  (snap["id"],)).fetchall()
                grouped = {}
                for row in rows:
                    event = grouped.setdefault(row["event_id"], {
                        "id": row["event_id"], "sport_key": snap["sport"], "sport_title": row["sport_title"],
                        "commence_time": row["commence_time"], "home_team": row["home_team"],
                        "away_team": row["away_team"], "source": "stored",
                        "retrieved_at": snap["fetched_at"], "bookmakers": []})
                    books = {b["key"]: b for b in event["bookmakers"]}
                    book = books.get(row["bookmaker_key"])
                    if book is None:
                        book = {"key": row["bookmaker_key"], "title": row["bookmaker_title"], "markets": []}
                        event["bookmakers"].append(book)
                    markets = {m["key"]: m for m in book["markets"]}
                    market = markets.get(row["market_key"])
                    if market is None:
                        market = {"key": row["market_key"], "outcomes": []}
                        book["markets"].append(market)
                    outcome = {"name": row["outcome_name"], "price": row["price"]}
                    if row["point"] is not None:
                        outcome["point"] = row["point"]
                    market["outcomes"].append(outcome)
                events.extend(grouped.values())
        return events

    def history(self, event_id, market_key, outcome_name, bookmaker_key=None, point=None, limit=100):
        sql = """SELECT s.fetched_at,q.bookmaker_key,q.bookmaker_title,q.market_key,
                 q.outcome_name,q.point,q.price FROM quotes q
                 JOIN snapshots s ON s.id=q.snapshot_id
                 WHERE q.event_id=? AND q.market_key=? AND q.outcome_name=?"""
        args = [event_id, market_key, outcome_name]
        if bookmaker_key:
            sql += " AND q.bookmaker_key=?"
            args.append(bookmaker_key)
        sql += " AND q.point IS NULL" if point is None else " AND q.point=?"
        if point is not None:
            args.append(point)
        sql += " ORDER BY s.fetched_at DESC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql, args).fetchall()]


    def movement(self, sport=None, limit=200):
        """Return the latest quote observation and its prior observation per market side."""
        sql = """
            WITH ranked AS (
                SELECT s.fetched_at, s.id AS snapshot_id, q.event_id, q.sport,
                       q.sport_title, q.commence_time, q.home_team, q.away_team,
                       q.bookmaker_key, q.bookmaker_title, q.market_key,
                       q.outcome_name, q.point, q.price,
                       ROW_NUMBER() OVER (
                           PARTITION BY q.event_id, q.bookmaker_key, q.market_key, q.outcome_name
                           ORDER BY s.fetched_at DESC, s.id DESC
                       ) AS rn
                FROM quotes q JOIN snapshots s ON s.id=q.snapshot_id
                WHERE (? IS NULL OR q.sport=?)
            )
            SELECT * FROM ranked WHERE rn <= 2
            ORDER BY event_id, bookmaker_key, market_key, outcome_name, rn
        """
        with self.connect() as db:
            rows = [dict(row) for row in db.execute(sql, (sport, sport)).fetchall()]
        grouped = {}
        for row in rows:
            key = (row["event_id"], row["bookmaker_key"], row["market_key"], row["outcome_name"])
            grouped.setdefault(key, []).append(row)
        results = []
        for observations in grouped.values():
            if len(observations) < 2:
                continue
            current, previous = observations[0], observations[1]
            old_price, new_price = previous["price"], current["price"]
            old_prob = 100 * (100 / (old_price + 100) if old_price > 0 else abs(old_price) / (abs(old_price) + 100))
            new_prob = 100 * (100 / (new_price + 100) if new_price > 0 else abs(new_price) / (abs(new_price) + 100))
            results.append({
                "event_id": current["event_id"], "sport": current["sport"],
                "matchup": f'{current["away_team"]} @ {current["home_team"]}',
                "commence_time": current["commence_time"], "market": current["market_key"],
                "outcome": current["outcome_name"], "bookmaker": current["bookmaker_title"],
                "bookmaker_key": current["bookmaker_key"],
                "previous_point": previous["point"], "point": current["point"],
                "previous_price": old_price, "price": new_price,
                "price_change": new_price - old_price,
                "implied_probability_change": round(new_prob - old_prob, 3),
                "previous_at": previous["fetched_at"], "updated_at": current["fetched_at"],
                "change_type": "line_and_price" if previous["point"] != current["point"] else "price",
            })
        results.sort(key=lambda x: x["updated_at"], reverse=True)
        return results[:max(1, min(int(limit), 1000))]


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
            return dict(row)
