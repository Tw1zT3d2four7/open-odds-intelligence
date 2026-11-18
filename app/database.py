import sqlite3
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
                    outcome_name TEXT NOT NULL, outcome_description TEXT NOT NULL DEFAULT '', point REAL, price INTEGER NOT NULL,
                    source_updated_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_quotes_lookup
                    ON quotes(event_id, market_key, outcome_name, point, bookmaker_key);
                CREATE INDEX IF NOT EXISTS idx_quotes_snapshot ON quotes(snapshot_id);
                CREATE TABLE IF NOT EXISTS sent_alerts (
                    fingerprint TEXT PRIMARY KEY, sent_at TEXT NOT NULL,
                    alert_type TEXT NOT NULL, summary TEXT NOT NULL
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
            """)
            quote_columns = {row["name"] for row in db.execute("PRAGMA table_info(quotes)")}
            if "source_updated_at" not in quote_columns:
                db.execute("ALTER TABLE quotes ADD COLUMN source_updated_at TEXT")
            if "outcome_description" not in quote_columns:
                db.execute("ALTER TABLE quotes ADD COLUMN outcome_description TEXT NOT NULL DEFAULT ''")

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
                                outcome["name"], outcome.get("description") or outcome.get("participant") or "",
                                outcome.get("point"), price,
                                outcome.get("source_updated_at") or outcome.get("updated_at") or
                                market.get("last_update") or book.get("last_update")))
            db.executemany("""INSERT INTO quotes(snapshot_id,event_id,sport,sport_title,commence_time,
                home_team,away_team,bookmaker_key,bookmaker_title,market_key,outcome_name,outcome_description,point,price,
                source_updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
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
                    if row["outcome_description"]:
                        outcome["description"] = row["outcome_description"]
                    if row["source_updated_at"]:
                        outcome["updated_at"] = row["source_updated_at"]
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
        """Persist the first positive-EV observation for each exact selection."""
        observed_at = observed_at or datetime.now(timezone.utc).isoformat()
        inserted = 0
        with self.connect() as db:
            for row in candidates:
                commence = row.get("commence_time")
                if not commence or str(row.get("market") or "").startswith("player_"):
                    continue
                try:
                    start = datetime.fromisoformat(str(commence).replace("Z", "+00:00"))
                    observed = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
                    if start.tzinfo is None:
                        start = start.replace(tzinfo=timezone.utc)
                    if observed.tzinfo is None:
                        observed = observed.replace(tzinfo=timezone.utc)
                    if observed >= start:
                        continue
                except (TypeError, ValueError):
                    continue
                point = row.get("point")
                point_key = "" if point is None else str(float(point))
                cur = db.execute(
                    """INSERT OR IGNORE INTO clv_entries(
                       event_id,sport,matchup,commence_time,market_key,outcome_name,
                       point,point_key,bookmaker_key,bookmaker_title,entry_price,
                       entry_fair_probability,entry_ev_percent,entry_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (str(row.get("event_id") or ""),
                     str(row.get("sport_key") or row.get("sport") or ""),
                     str(row.get("matchup") or ""), str(commence), str(row.get("market") or ""),
                     str(row.get("outcome") or ""), point, point_key,
                     str(row.get("bookmaker_key") or ""), str(row.get("bookmaker") or ""),
                     int(row["price"]), float(row["fair_probability"]),
                     float(row["ev_percent"]), observed_at))
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
                if (len(set(identities)) != len(identities) or
                    any(not isinstance(o["price"], int) or o["price"] == 0 for o in outcomes)):
                    continue
                valid_books[book] = (tuple(sorted(identities, key=lambda item: (item[0], str(item[1])))), outcomes)
            # Only compare complete peer markets with identical outcome/line sets.
            identity_counts = {}
            for identities, outcomes in valid_books.values():
                identity_counts[identities] = identity_counts.get(identities, 0) + 1
            common = max(identity_counts, key=identity_counts.get) if identity_counts else None
            peer_probabilities = []
            if common is not None and identity_counts[common] >= 2:
                for identities, outcomes in valid_books.values():
                    if identities != common:
                        continue
                    probs = no_vig_probabilities([o["price"] for o in outcomes])
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
                    entry_decimal = american_to_decimal(int(entry["entry_price"]))
                    clv = (entry_decimal / close_decimal - 1) * 100
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
            return dict(row)
