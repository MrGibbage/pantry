"""SQLite storage for Pantry's working state (the current week's picks, list decisions,
requests and ratings). The grocery repo stays the long-term record; Claude copies decisions
there with `pantry dump`."""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

DB_PATH = os.environ.get("PANTRY_DB", "/data/pantry.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS weeks (
  id TEXT PRIMARY KEY,              -- shopping day, YYYY-MM-DD
  status TEXT NOT NULL DEFAULT 'picking',  -- picking | listing | review | shopping | done
  note TEXT NOT NULL DEFAULT '',    -- free-text from the household for Claude
  claude_note TEXT NOT NULL DEFAULT '',  -- what Claude wants to say about this week
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proposals (
  id INTEGER PRIMARY KEY,
  week_id TEXT NOT NULL REFERENCES weeks(id),
  position INTEGER NOT NULL DEFAULT 0,
  slug TEXT NOT NULL,
  name TEXT NOT NULL,
  recipe_id TEXT NOT NULL DEFAULT '',
  kcal REAL,
  protein REAL,
  tags TEXT NOT NULL DEFAULT '[]',
  reason TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'claude',   -- claude | household
  picked INTEGER NOT NULL DEFAULT 0,
  picked_by TEXT NOT NULL DEFAULT '',
  picked_at TEXT NOT NULL DEFAULT '',
  cooked_at TEXT NOT NULL DEFAULT '',      -- ticked "made it" on the home page
  UNIQUE (week_id, slug)
);
CREATE TABLE IF NOT EXISTS requests (
  id INTEGER PRIMARY KEY,
  week_id TEXT NOT NULL REFERENCES weeks(id),
  text TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  slug TEXT NOT NULL DEFAULT '',
  name TEXT NOT NULL DEFAULT '',
  recipe_id TEXT NOT NULL DEFAULT '',
  by TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',     -- open | done
  answer TEXT NOT NULL DEFAULT ''          -- Claude's reply, shown under the request
);
CREATE TABLE IF NOT EXISTS list_items (
  id INTEGER PRIMARY KEY,
  week_id TEXT NOT NULL REFERENCES weeks(id),
  position INTEGER NOT NULL DEFAULT 0,
  section TEXT NOT NULL,                   -- recipes | staples | usual | onhand | extra
  name TEXT NOT NULL,
  qty TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT '',
  recipes TEXT NOT NULL DEFAULT '[]',      -- recipe names/slugs that need it
  default_decision TEXT NOT NULL DEFAULT '',  -- need | have | ''
  decision TEXT NOT NULL DEFAULT '',          -- need | have | ''
  decided_by TEXT NOT NULL DEFAULT '',
  decided_at TEXT NOT NULL DEFAULT '',
  mealie_item_id TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS ratings (
  week_id TEXT NOT NULL,
  slug TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  rating INTEGER NOT NULL DEFAULT 0,       -- 1 | -1 | 0
  note TEXT NOT NULL DEFAULT '',
  by TEXT NOT NULL DEFAULT '',
  at TEXT NOT NULL,
  PRIMARY KEY (week_id, slug)
);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,                      -- plan_week
  week_id TEXT NOT NULL DEFAULT '',
  requested_by TEXT NOT NULL DEFAULT '',
  requested_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',          -- household's note for Claude
  status TEXT NOT NULL DEFAULT 'queued',   -- queued | running | done | failed
  started_at TEXT NOT NULL DEFAULT '',
  finished_at TEXT NOT NULL DEFAULT '',
  message TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  week_id TEXT NOT NULL DEFAULT '',
  who TEXT NOT NULL DEFAULT '',
  action TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT ''
);
"""

STATUSES = ["picking", "listing", "review", "shopping", "done"]


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA journal_mode = WAL")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init():
    with conn() as c:
        c.executescript(SCHEMA)
        cols = {r[1] for r in c.execute("PRAGMA table_info(proposals)")}
        if "cooked_at" not in cols:
            c.execute("ALTER TABLE proposals ADD COLUMN cooked_at TEXT NOT NULL DEFAULT ''")


def log(c, action, week_id="", who="", detail=""):
    if not isinstance(detail, str):
        detail = json.dumps(detail, ensure_ascii=False)
    c.execute("INSERT INTO events (at, week_id, who, action, detail) VALUES (?,?,?,?,?)",
              (now(), week_id, who, action, detail))


def current_week(c):
    """The newest week that isn't done, else the newest week, else None."""
    r = c.execute("SELECT * FROM weeks WHERE status != 'done' ORDER BY id DESC LIMIT 1").fetchone()
    return r or c.execute("SELECT * FROM weeks ORDER BY id DESC LIMIT 1").fetchone()


def cooking_week(c, today):
    """The week being cooked now: the newest shopped week (shopping or done) whose shop day
    is today or earlier, if it was within the last two weeks."""
    return c.execute(
        "SELECT * FROM weeks WHERE status IN ('shopping','done') AND id <= ? AND id >= ? "
        "ORDER BY id DESC LIMIT 1",
        (today.isoformat(), (today - timedelta(days=13)).isoformat())).fetchone()


def rows(cur):
    out = []
    for r in cur.fetchall():
        d = dict(r)
        for k in ("tags", "recipes"):
            if k in d and isinstance(d[k], str):
                try:
                    d[k] = json.loads(d[k])
                except ValueError:
                    d[k] = []
        out.append(d)
    return out
