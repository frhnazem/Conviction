"""SQLite locally; Postgres when DATABASE_URL is set (Railway)."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
from urllib.parse import urlparse, unquote

FREE_TIER_LIMIT = 10
FREE_TIER_MESSAGE = "Free tier is 10 logs. Upgrade later for unlimited."

DATABASE_URL = (os.environ.get("DATABASE_URL") or "").strip()
SQLITE_PATH = os.environ.get("SQLITE_PATH") or os.path.join(os.path.dirname(__file__), "conviction.db")


def _is_postgres() -> bool:
    return DATABASE_URL.startswith("postgres")


def _pg_dsn() -> str:
    url = DATABASE_URL
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    return url


def _convert_placeholders(sql: str) -> str:
    if not _is_postgres():
        return sql
    out = []
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch == "?":
            out.append("%s")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


@contextmanager
def get_conn():
    if _is_postgres():
        import psycopg2
        import psycopg2.extras

        conn = psycopg2.connect(_pg_dsn())
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        conn = sqlite3.connect(SQLITE_PATH)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def query(sql: str, params: tuple | list = ()):
    sql = _convert_placeholders(sql)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
        if _is_postgres():
            cols = [d[0] for d in cur.description] if cur.description else []
            return [dict(zip(cols, r)) for r in rows]
        return [dict(r) for r in rows]


def execute(sql: str, params: tuple | list = (), returning: str | None = None):
    sql = _convert_placeholders(sql)
    with get_conn() as conn:
        cur = conn.cursor()
        if returning and _is_postgres():
            cur.execute(sql + " RETURNING " + returning, params)
            row = cur.fetchone()
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row)) if row else None
        cur.execute(sql, params)
        if returning and not _is_postgres():
            if returning.strip() == "id":
                return {"id": cur.lastrowid}
            last = cur.lastrowid
            return {"id": last}
        return {"rowcount": cur.rowcount}


def execute_script(statements: list[str]) -> None:
    with get_conn() as conn:
        cur = conn.cursor()
        for stmt in statements:
            stmt = stmt.strip()
            if not stmt:
                continue
            try:
                cur.execute(stmt)
            except Exception as err:
                msg = str(err).lower()
                if "duplicate" in msg or "already exists" in msg:
                    continue
                raise


def _add_column(table: str, column: str, ddl: str) -> None:
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            if _is_postgres():
                cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl}")
            else:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    except Exception as err:
        msg = str(err).lower()
        if "duplicate" in msg or "already exists" in msg:
            return
        raise


def init_db() -> None:
    id_pk = "SERIAL PRIMARY KEY" if _is_postgres() else "INTEGER PRIMARY KEY AUTOINCREMENT"
    ts_default = "TIMESTAMPTZ NOT NULL DEFAULT NOW()" if _is_postgres() else "TEXT NOT NULL DEFAULT (datetime('now'))"
    statements = [
        f"""
        CREATE TABLE IF NOT EXISTS users (
          id {id_pk},
          email TEXT NOT NULL UNIQUE,
          password_hash TEXT NOT NULL,
          focus_theme TEXT NOT NULL DEFAULT 'auto',
          created_at {ts_default}
        )
        """,
        f"""
        CREATE TABLE IF NOT EXISTS decisions (
          id {id_pk},
          user_id INTEGER NOT NULL,
          created_at {ts_default},
          raw_text TEXT NOT NULL,
          pair TEXT,
          bias TEXT,
          invalidation TEXT,
          target TEXT,
          size_note TEXT,
          action TEXT NOT NULL,
          emotion TEXT,
          parse_confidence DOUBLE PRECISION,
          status TEXT NOT NULL DEFAULT 'open',
          invalidation_result TEXT,
          trade_result TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS decisions_user_created_idx ON decisions (user_id, created_at DESC)",
        f"""
        CREATE TABLE IF NOT EXISTS user_rules (
          id {id_pk},
          user_id INTEGER NOT NULL,
          pattern_id TEXT NOT NULL,
          rule_text TEXT NOT NULL,
          status TEXT NOT NULL,
          created_at {ts_default},
          UNIQUE (user_id, pattern_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS user_rules_user_status_idx ON user_rules (user_id, status)",
        f"""
        CREATE TABLE IF NOT EXISTS demo_feedback (
          id {id_pk},
          user_id INTEGER,
          logged_more_than_once TEXT,
          rule_made_sense TEXT,
          warning_help_or_annoy TEXT,
          confusing TEXT,
          use_next_week TEXT,
          ideas TEXT,
          created_at {ts_default}
        )
        """,
        "CREATE INDEX IF NOT EXISTS demo_feedback_created_at_idx ON demo_feedback (created_at DESC)",
        f"""
        CREATE TABLE IF NOT EXISTS product_memory (
          id {id_pk},
          record_type TEXT NOT NULL,
          title TEXT NOT NULL,
          body TEXT NOT NULL,
          source TEXT,
          created_at {ts_default}
        )
        """,
        "CREATE UNIQUE INDEX IF NOT EXISTS product_memory_title_idx ON product_memory (title)",
    ]
    execute_script(statements)
    _add_column("users", "focus_theme", "TEXT NOT NULL DEFAULT 'auto'")
    _add_column("decisions", "invalidation_result", "TEXT")
    _add_column("decisions", "trade_result", "TEXT")
    seed_product_memory()


MEMORY_SEED = [
    (
        "pattern",
        "fomo_no_invalidation",
        "Buy logs tagged FOMO with missing invalidation. Rule: No Buy without invalidation when emotion is FOMO.",
        "catalog",
    ),
    (
        "pattern",
        "rushed_buy_no_invalidation",
        "Buy logs tagged Rushed with missing invalidation. Rule: No Buy without invalidation when emotion is Rushed.",
        "catalog",
    ),
    (
        "pattern",
        "ignore_invalidation_then_loss",
        "Logs marked invalidation ignored and result as Loss. Rule: When invalidation is hit, do not ignore it — exit or reduce instead of holding.",
        "catalog",
    ),
    (
        "pattern",
        "revenge_after_loss",
        "A Buy within 24 hours after a Loss. Rule: After a Loss, no new Buy for 24 hours.",
        "catalog",
    ),
    (
        "pattern",
        "buy_missing_invalidation",
        "Buy logs missing invalidation. Rule: Require an invalidation level before every Buy.",
        "catalog",
    ),
    (
        "policy",
        "automatic one-rule priority",
        "The app suggests one habit at a time. Automatic priority: FOMO, then Rushed, then ignore-invalidation-then-loss, then revenge-after-loss, then buy-missing-invalidation. An optional weekly focus theme may prefer that family when it qualifies. Accepting a rule replaces the previous accepted rule. The user does not rank multiple rules.",
        "product",
    ),
    (
        "policy",
        "delete policy",
        "Deleted decision logs are gone. There is no shadow archive of deleted logs for hidden self-learning. Feedback is stored separately for product development and is not a public feed.",
        "product",
    ),
]


def seed_product_memory() -> None:
    for record_type, title, body, source in MEMORY_SEED:
        existing = query("SELECT id FROM product_memory WHERE title = ? LIMIT 1", (title,))
        if existing:
            continue
        execute(
            "INSERT INTO product_memory (record_type, title, body, source) VALUES (?, ?, ?, ?)",
            (record_type, title, body, source),
        )


DEMO_NOTES = [
    ("[demo] fomo buy eth, no stop, chasing the green candle", "ETHUSDT", "long", None, None, None, "buy", "FOMO", 0.7),
    ("[demo] skip sol, flat, nothing to do today", "SOLUSDT", "flat", None, None, None, "skip", "Calm", 0.8),
    ("[demo] reduce btc size, rushed after the wick", "BTCUSDT", "long", "94000", None, "cut in half", "reduce", "Rushed", 0.75),
    ("[demo] long btc above 95k, stop 93500, targeting 102", "BTCUSDT", "long", "93500", "102", None, "buy", "Calm", 0.9),
    ("[demo] hold eth, stop still 3200", "ETHUSDT", "long", "3200", None, None, "hold", "Calm", 0.7),
    ("[demo] skip pepe, chart looks weird, fomo lingering", "PEPEUSDT", "flat", None, None, None, "skip", "FOMO", 0.6),
]


def ensure_demo_seed(user_id: int) -> None:
    n = query("SELECT COUNT(*) AS n FROM decisions WHERE user_id = ?", (user_id,))[0]["n"]
    if n >= 6:
        return
    room = max(0, FREE_TIER_LIMIT - int(n))
    need = min(6 - int(n), room)
    now = datetime.now(timezone.utc)
    for i, row in enumerate(DEMO_NOTES[:need]):
        created = (now - timedelta(hours=12 + i * 18)).isoformat()
        execute(
            """
            INSERT INTO decisions (
              user_id, created_at, raw_text, pair, bias, invalidation, target, size_note,
              action, emotion, parse_confidence, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'demo')
            """,
            (user_id, created, *row),
        )
