import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DIFFICULTIES = ("easy", "medium", "hard")


class Store:
    def __init__(self, path, history_days):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.history_seconds = history_days * 86400
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS puzzles (
                    id TEXT PRIMARY KEY,
                    difficulty TEXT NOT NULL CHECK(difficulty IN ('easy','medium','hard')),
                    rating INTEGER NOT NULL,
                    added_at REAL NOT NULL,
                    reserved_post TEXT,
                    used_at REAL
                );
                CREATE TABLE IF NOT EXISTS subscribers (user_id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS posts (
                    id TEXT PRIMARY KEY,
                    channel TEXT NOT NULL,
                    body TEXT NOT NULL,
                    notifications TEXT NOT NULL,
                    thread_ts TEXT,
                    notified INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS solves (
                    day TEXT NOT NULL,
                    difficulty TEXT NOT NULL CHECK(difficulty IN ('easy','medium','hard')),
                    user_id TEXT NOT NULL,
                    post_id TEXT NOT NULL,
                    place INTEGER NOT NULL CHECK(place > 0),
                    announced INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(day, difficulty, user_id),
                    UNIQUE(day, difficulty, place)
                );
            """)
            for table, old, new in (("posts", "day", "id"),
                                    ("puzzles", "reserved_day", "reserved_post")):
                if old in {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}:
                    db.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(puzzles)")}
            for name in ("fen", "first_move", "preview_file_id"):
                if name not in columns:
                    db.execute(f"ALTER TABLE puzzles ADD COLUMN {name} TEXT")
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='scheduled_solves'").fetchone():
                db.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE scheduled_solves (
                        post_id TEXT NOT NULL,
                        difficulty TEXT NOT NULL CHECK(difficulty IN ('easy','medium','hard')),
                        user_id TEXT NOT NULL,
                        place INTEGER NOT NULL CHECK(place BETWEEN 1 AND 3),
                        nickname TEXT,
                        PRIMARY KEY(post_id, difficulty, user_id),
                        UNIQUE(post_id, difficulty, place)
                    );
                    INSERT INTO scheduled_solves(post_id,difficulty,user_id,place)
                    SELECT post_id,difficulty,user_id,place FROM (
                        SELECT post_id,difficulty,user_id,
                               row_number() OVER (PARTITION BY post_id,difficulty ORDER BY place) AS place
                        FROM solves WHERE post_id NOT LIKE 'manual:%'
                    ) WHERE place<=3;
                """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            db.close()

    def add(self, puzzle, difficulty, now):
        with self.connect() as db:
            db.execute("DELETE FROM puzzles WHERE used_at <= ?",
                       (now - self.history_seconds,))
            return db.execute(
                "INSERT OR IGNORE INTO puzzles(id,difficulty,rating,added_at,fen,first_move) VALUES (?,?,?,?,?,?)",
                (puzzle.id, difficulty, puzzle.rating, now, puzzle.fen, puzzle.first_move),
            ).rowcount == 1

    def posted_puzzle(self, identifier):
        with self.connect() as db:
            return db.execute(
                "SELECT puzzles.*, posts.channel FROM puzzles JOIN posts ON posts.id=reserved_post "
                "WHERE puzzles.id=? AND posts.thread_ts IS NOT NULL AND used_at IS NOT NULL",
                (identifier,),
            ).fetchone()

    def cache_answer(self, puzzle):
        with self.connect() as db:
            db.execute("UPDATE puzzles SET fen=?, first_move=? WHERE id=?",
                       (puzzle.fen, puzzle.first_move, puzzle.id))

    def cache_preview(self, identifier, file_id):
        with self.connect() as db:
            db.execute("UPDATE puzzles SET preview_file_id=? WHERE id=?", (file_id, identifier))

    def solve(self, day, difficulty, user_id, post_id, nickname=None):
        with self.connect() as db:
            if not post_id.startswith("manual:"):
                place = db.execute(
                    "SELECT count(*)+1 FROM scheduled_solves WHERE post_id=? AND difficulty=?",
                    (post_id, difficulty),
                ).fetchone()[0]
                if place <= 3:
                    db.execute(
                        "INSERT OR IGNORE INTO scheduled_solves(post_id,difficulty,user_id,place,nickname) "
                        "VALUES (?,?,?,?,?)", (post_id, difficulty, user_id, place, nickname),
                    )
            previous = db.execute("SELECT place FROM solves WHERE day=? AND difficulty=? AND user_id=?",
                                  (day, difficulty, user_id)).fetchone()
            if previous:
                return previous["place"], False
            place = db.execute("SELECT count(*)+1 FROM solves WHERE day=? AND difficulty=?",
                               (day, difficulty)).fetchone()[0]
            db.execute("INSERT INTO solves(day,difficulty,user_id,post_id,place) VALUES (?,?,?,?,?)",
                       (day, difficulty, user_id, post_id, place))
            return place, True

    def scheduled_ranking(self, post_id):
        with self.connect() as db:
            return db.execute("SELECT * FROM scheduled_solves WHERE post_id=? ORDER BY difficulty,place",
                              (post_id,)).fetchall()

    def pending_solves(self):
        with self.connect() as db:
            return db.execute(
                "SELECT solves.*, posts.channel, posts.thread_ts FROM solves JOIN posts ON posts.id=post_id "
                "WHERE announced=0 AND place<=3 ORDER BY day, difficulty, place",
            ).fetchall()

    def announced(self, solve):
        with self.connect() as db:
            db.execute("UPDATE solves SET announced=1 WHERE day=? AND difficulty=? AND user_id=?",
                       (solve["day"], solve["difficulty"], solve["user_id"]))

    def has_queued(self, difficulty):
        with self.connect() as db:
            return db.execute(
                "SELECT 1 FROM puzzles WHERE difficulty=? AND reserved_post IS NULL LIMIT 1",
                (difficulty,),
            ).fetchone() is not None

    def subscribe(self, user_id, enabled):
        with self.connect() as db:
            if enabled:
                db.execute("INSERT OR IGNORE INTO subscribers VALUES (?)", (user_id,))
            else:
                db.execute("DELETE FROM subscribers WHERE user_id=?", (user_id,))

    def post(self, post_id):
        with self.connect() as db:
            return db.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()

    def selected(self, post_id):
        with self.connect() as db:
            puzzles = db.execute("SELECT * FROM puzzles WHERE reserved_post=?", (post_id,)).fetchall()
        return sorted(puzzles, key=lambda puzzle: DIFFICULTIES.index(puzzle["difficulty"]))

    def pending(self, *, manual=False):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM posts WHERE (id LIKE 'manual:%')=? ORDER BY rowid",
                              (manual,)).fetchall()
        return [row for row in rows if not row["thread_ts"] or
                row["notified"] < len(json.loads(row["notifications"]))]

    def prepare(self, day, channel, template, *, post_id=None):
        post_id = post_id or day
        with self.connect() as db:
            if db.execute("SELECT 1 FROM posts WHERE id=?", (post_id,)).fetchone():
                return
            values = {"date": day}
            for difficulty in DIFFICULTIES:
                puzzle = db.execute(
                    "SELECT * FROM puzzles WHERE difficulty=? AND reserved_post IS NULL "
                    "ORDER BY added_at, rowid LIMIT 1", (difficulty,),
                ).fetchone()
                if puzzle is None:
                    raise ValueError(f"The {difficulty} bin is empty.")
                values[f"{difficulty}_rating"] = puzzle["rating"]
                db.execute("UPDATE puzzles SET reserved_post=? WHERE id=?", (post_id, puzzle["id"]))
            body = template.format(**values)
            if not body.strip() or len(body) > 3000:
                raise ValueError("Daily message must contain 1–3000 characters.")
            users = [f"<@{row[0]}>" for row in db.execute("SELECT user_id FROM subscribers ORDER BY user_id")]
            notifications = ["It's time for daily chess! " + " ".join(users[i:i + 50])
                             for i in range(0, len(users), 50)]
            db.execute("INSERT INTO posts(id,channel,body,notifications) VALUES (?,?,?,?)",
                       (post_id, channel, body, json.dumps(notifications)))

    def posted(self, post_id, thread_ts, now):
        with self.connect() as db:
            db.execute("UPDATE posts SET thread_ts=? WHERE id=?", (thread_ts, post_id))
            db.execute("UPDATE puzzles SET used_at=? WHERE reserved_post=? AND used_at IS NULL",
                       (now, post_id))

    def notified(self, post_id, count):
        with self.connect() as db:
            db.execute("UPDATE posts SET notified=? WHERE id=?", (count, post_id))
