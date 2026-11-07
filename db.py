"""
db.py — Dual-backend async database layer.

Backend selection (auto at startup via environment variables)
──────────────────────────────────────────────────────────────
  TURSO_DATABASE_URL + TURSO_AUTH_TOKEN both set
      → libsql-client  (Turso cloud database — persistent across all Render deploys)
  Otherwise
      → aiosqlite      (local .db file — development only)

All public functions expose an identical API regardless of which backend is active.
Only the internals switch; callers never need to know.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("cf-bot.db")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TURSO_URL   = os.getenv("TURSO_DATABASE_URL", "").strip()
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN",   "").strip()
DB_PATH     = os.getenv("DB_PATH", "codeforces_bot.db")   # only used for local fallback

_USE_TURSO: bool = bool(TURSO_URL and TURSO_TOKEN)

# ---------------------------------------------------------------------------
# Conditional imports
# ---------------------------------------------------------------------------

if _USE_TURSO:
    try:
        import libsql_client  # pip install libsql-client
    except ImportError as _exc:
        raise ImportError(
            "TURSO_DATABASE_URL and TURSO_AUTH_TOKEN are set, but the "
            "'libsql-client' package is not installed.\n"
            "Run:  pip install libsql-client"
        ) from _exc
    _turso_client: "libsql_client.Client | None" = None
else:
    import aiosqlite  # type: ignore[import]

# ---------------------------------------------------------------------------
# DDL — identical for both backends (libSQL is SQLite-compatible)
# ---------------------------------------------------------------------------

_DDL: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS users (
        discord_id    TEXT PRIMARY KEY,
        cf_handle     TEXT UNIQUE NOT NULL COLLATE NOCASE,
        total_points  INTEGER NOT NULL DEFAULT 0,
        registered_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS solved_problems (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        cf_handle      TEXT    NOT NULL COLLATE NOCASE,
        problem_key    TEXT    NOT NULL,
        rating         INTEGER,
        points_awarded INTEGER NOT NULL DEFAULT 0,
        solved_at      TEXT    NOT NULL,
        UNIQUE(cf_handle, problem_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_solved_handle ON solved_problems(cf_handle COLLATE NOCASE)",
]

# ---------------------------------------------------------------------------
# Turso-specific helpers (only evaluated when _USE_TURSO is True)
# ---------------------------------------------------------------------------

def _get_turso() -> "libsql_client.Client":
    """Return the shared Turso client, creating it on first call."""
    global _turso_client
    if _turso_client is None:
        _turso_client = libsql_client.create_client(
            url=TURSO_URL,
            auth_token=TURSO_TOKEN,
        )
    return _turso_client


def _stmt(sql: str, params: list[Any] | None = None) -> "libsql_client.Statement":
    return libsql_client.Statement(sql, params or [])


def _to_dicts(rs: "libsql_client.ResultSet") -> list[dict[str, Any]]:
    cols = rs.columns
    return [dict(zip(cols, row)) for row in rs.rows]


def _is_constraint_error(exc: Exception) -> bool:
    msg = str(exc).upper()
    return "UNIQUE" in msg or "CONSTRAINT" in msg

# ---------------------------------------------------------------------------
# Backend-agnostic internal primitives
# ---------------------------------------------------------------------------

async def _rows(sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    """Execute a SELECT and return a list of row-dicts."""
    if _USE_TURSO:
        rs = await _get_turso().execute(_stmt(sql, params))
        return _to_dicts(rs)
    # ── aiosqlite ───────────────────────────────────────────────────────
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params or []) as cur:
            return [dict(r) for r in await cur.fetchall()]


async def _one(sql: str, params: list[Any] | None = None) -> dict[str, Any] | None:
    """Execute a SELECT and return the first row-dict, or None."""
    rows = await _rows(sql, params)
    return rows[0] if rows else None


async def _exec(sql: str, params: list[Any] | None = None) -> None:
    """Execute a single write statement (raises on any error)."""
    if _USE_TURSO:
        await _get_turso().execute(_stmt(sql, params))
    else:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(sql, params or [])
            await db.commit()


async def _batch(stmts: list[tuple[str, list[Any] | None]]) -> bool:
    """
    Execute multiple ``(sql, params)`` pairs atomically.

    Returns
    -------
    True  — all statements succeeded.
    False — a UNIQUE / constraint error occurred (transaction rolled back).
    Raises for any other error.
    """
    if _USE_TURSO:
        try:
            await _get_turso().batch([_stmt(sql, p) for sql, p in stmts])
            return True
        except Exception as exc:
            if _is_constraint_error(exc):
                return False
            raise
    # ── aiosqlite ───────────────────────────────────────────────────────
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            for sql, params in stmts:
                await db.execute(sql, params or [])
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

async def init_db() -> None:
    """Create tables and indexes (idempotent — safe to call on every startup)."""
    if _USE_TURSO:
        await _get_turso().batch([_stmt(ddl) for ddl in _DDL])
        log.info("Turso cloud database ready.  URL: %s", TURSO_URL.split("?")[0])
    else:
        async with aiosqlite.connect(DB_PATH) as db:
            for stmt in _DDL:
                await db.execute(stmt)
            await db.commit()
        log.info("Local SQLite database ready at: %s", DB_PATH)


async def close_db() -> None:
    """Close the Turso client gracefully (called in CFBot.close())."""
    global _turso_client
    if _USE_TURSO and _turso_client is not None:
        await _turso_client.close()
        _turso_client = None
        log.info("Turso client closed.")

# ---------------------------------------------------------------------------
# User helpers
# ---------------------------------------------------------------------------

async def get_user_by_discord_id(discord_id: str) -> dict[str, Any] | None:
    return await _one(
        "SELECT * FROM users WHERE discord_id = ?", [discord_id]
    )


async def get_user_by_cf_handle(cf_handle: str) -> dict[str, Any] | None:
    return await _one(
        "SELECT * FROM users WHERE cf_handle = ?", [cf_handle]
    )


async def link_user(discord_id: str, cf_handle: str) -> None:
    """
    Insert or update the user row (upsert).
    Re-linking to a new handle preserves discord_id; points stay in DB.
    """
    now = datetime.now(timezone.utc).isoformat()
    await _exec(
        """
        INSERT INTO users (discord_id, cf_handle, total_points, registered_at)
        VALUES (?, ?, 0, ?)
        ON CONFLICT(discord_id) DO UPDATE
            SET cf_handle     = excluded.cf_handle,
                registered_at = excluded.registered_at
        """,
        [discord_id, cf_handle, now],
    )

# ---------------------------------------------------------------------------
# Solved-problem helpers
# ---------------------------------------------------------------------------

async def add_solved_problem(
    cf_handle:   str,
    problem_key: str,
    rating:      int | None,
    points:      int,
    solved_at:   str,
) -> bool:
    """
    Insert a solved problem and credit points to the user atomically.

    Returns
    -------
    True  — newly inserted (points credited).
    False — duplicate (UNIQUE constraint); nothing changed.
    """
    return await _batch([
        (
            """
            INSERT INTO solved_problems (cf_handle, problem_key, rating, points_awarded, solved_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            [cf_handle, problem_key, rating, points, solved_at],
        ),
        (
            "UPDATE users SET total_points = total_points + ? WHERE cf_handle = ?",
            [points, cf_handle],
        ),
    ])


async def get_all_solved_keys(cf_handle: str) -> set[str]:
    """Return the set of all problem_keys already stored for this handle."""
    rows = await _rows(
        "SELECT problem_key FROM solved_problems WHERE cf_handle = ?", [cf_handle]
    )
    return {r["problem_key"] for r in rows}


async def get_all_linked_users() -> list[dict[str, Any]]:
    """Return every row in the users table (discord_id + cf_handle)."""
    return await _rows("SELECT discord_id, cf_handle FROM users")

# ---------------------------------------------------------------------------
# Leaderboard (all-time)
# ---------------------------------------------------------------------------

async def get_leaderboard(limit: int = 15) -> list[dict[str, Any]]:
    """
    Return top ``limit`` users ordered by total_points descending.
    Each row: discord_id, cf_handle, total_points, solved_count.
    """
    return await _rows(
        """
        SELECT
            u.discord_id,
            u.cf_handle,
            u.total_points,
            COUNT(s.id) AS solved_count
        FROM users u
        LEFT JOIN solved_problems s ON s.cf_handle = u.cf_handle
        GROUP BY u.discord_id
        ORDER BY u.total_points DESC
        LIMIT ?
        """,
        [limit],
    )

# ---------------------------------------------------------------------------
# Daily leaderboard (last 24 hours)
# ---------------------------------------------------------------------------

async def get_daily_leaderboard(limit: int = 10) -> list[dict[str, Any]]:
    """
    Return top ``limit`` users by **points earned in the last 24 hours**.

    Sort order
    ----------
    1. ``daily_points``     DESC  (primary)
    2. ``daily_rating_sum`` DESC  (tie-breaker — sum of CF ratings solved today)

    Each row: discord_id, cf_handle, daily_points, daily_solved,
              daily_rating_sum, top_problem_key, top_problem_rating.

    Implementation note on solved_at comparison
    ─────────────────────────────────────────────
    Timestamps are stored as ISO-8601 strings with timezone suffix
    (e.g. '2026-11-05T17:04:21+00:00').  libSQL/SQLite's datetime()
    does not parse timezone offsets in input strings, so we normalise
    via REPLACE(SUBSTR(solved_at,1,19),'T',' ') before comparing with
    datetime('now','-24 hours').
    """
    return await _rows(
        """
        SELECT
            u.discord_id,
            s.cf_handle,
            SUM(s.points_awarded)         AS daily_points,
            COUNT(s.id)                   AS daily_solved,
            SUM(COALESCE(s.rating, 0))    AS daily_rating_sum,
            (
                SELECT s2.problem_key
                FROM   solved_problems s2
                WHERE  s2.cf_handle = s.cf_handle
                  AND  REPLACE(SUBSTR(s2.solved_at, 1, 19), 'T', ' ')
                           >= datetime('now', '-24 hours')
                ORDER BY COALESCE(s2.rating, 0) DESC
                LIMIT 1
            ) AS top_problem_key,
            (
                SELECT s2.rating
                FROM   solved_problems s2
                WHERE  s2.cf_handle = s.cf_handle
                  AND  REPLACE(SUBSTR(s2.solved_at, 1, 19), 'T', ' ')
                           >= datetime('now', '-24 hours')
                ORDER BY COALESCE(s2.rating, 0) DESC
                LIMIT 1
            ) AS top_problem_rating
        FROM  solved_problems s
        JOIN  users u ON u.cf_handle = s.cf_handle
        WHERE REPLACE(SUBSTR(s.solved_at, 1, 19), 'T', ' ')
                  >= datetime('now', '-24 hours')
        GROUP BY s.cf_handle
        ORDER BY daily_points DESC, daily_rating_sum DESC
        LIMIT ?
        """,
        [limit],
    )

# ---------------------------------------------------------------------------
# User stats
# ---------------------------------------------------------------------------

async def get_user_stats(cf_handle: str) -> dict[str, Any] | None:
    """
    Return extended statistics for a single user:
      - All columns from users
      - solved_count
      - rating_distribution  {bucket_label: count}
    """
    user = await _one("SELECT * FROM users WHERE cf_handle = ?", [cf_handle])
    if user is None:
        return None

    count_rows = await _rows(
        "SELECT COUNT(*) AS cnt FROM solved_problems WHERE cf_handle = ?", [cf_handle]
    )
    user["solved_count"] = count_rows[0]["cnt"] if count_rows else 0

    dist_rows = await _rows(
        """
        SELECT
            CASE
                WHEN rating IS NULL               THEN 'Unrated'
                WHEN rating < 800                 THEN '<800'
                WHEN rating BETWEEN 800  AND 999  THEN '800–999'
                WHEN rating BETWEEN 1000 AND 1199 THEN '1000–1199'
                WHEN rating BETWEEN 1200 AND 1399 THEN '1200–1399'
                WHEN rating BETWEEN 1400 AND 1599 THEN '1400–1599'
                WHEN rating BETWEEN 1600 AND 1799 THEN '1600–1799'
                WHEN rating BETWEEN 1800 AND 1999 THEN '1800–1999'
                WHEN rating BETWEEN 2000 AND 2399 THEN '2000–2399'
                ELSE '2400+'
            END AS bucket,
            COUNT(*) AS cnt
        FROM solved_problems
        WHERE cf_handle = ?
        GROUP BY bucket
        """,
        [cf_handle],
    )
    user["rating_distribution"] = {r["bucket"]: r["cnt"] for r in dist_rows}

    return user
