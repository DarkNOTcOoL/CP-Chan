"""
db.py — Async SQLite database layer (aiosqlite).

Tables
------
users           : discord_id, cf_handle, total_points, registered_at
solved_problems : cf_handle, problem_key, rating, points_awarded, solved_at
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import aiosqlite

log = logging.getLogger("cf-bot.db")

DB_PATH: str = os.getenv("DB_PATH", "codeforces_bot.db")

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_DDL = [
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
        problem_key    TEXT    NOT NULL,          -- e.g. '213A'
        rating         INTEGER,
        points_awarded INTEGER NOT NULL DEFAULT 0,
        solved_at      TEXT    NOT NULL,
        UNIQUE(cf_handle, problem_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_solved_handle ON solved_problems(cf_handle COLLATE NOCASE)",
]


async def init_db() -> None:
    """Create tables and indexes if they don't exist."""
    async with aiosqlite.connect(DB_PATH) as db:
        for stmt in _DDL:
            await db.execute(stmt)
        await db.commit()
    log.info("Database initialised at %s", DB_PATH)


# ---------------------------------------------------------------------------
# User helpers
# ---------------------------------------------------------------------------

async def get_user_by_discord_id(discord_id: str) -> dict[str, Any] | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE discord_id = ?", (discord_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_user_by_cf_handle(cf_handle: str) -> dict[str, Any] | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM users WHERE cf_handle = ?", (cf_handle,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def link_user(discord_id: str, cf_handle: str) -> None:
    """
    Insert or update the user row.
    Re-linking the same Discord account to a new handle resets the handle
    but preserves points (points are recalculated from solved_problems separately).
    """
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users (discord_id, cf_handle, total_points, registered_at)
            VALUES (?, ?, 0, ?)
            ON CONFLICT(discord_id) DO UPDATE
                SET cf_handle     = excluded.cf_handle,
                    registered_at = excluded.registered_at
            """,
            (discord_id, cf_handle, now),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Solved-problem helpers
# ---------------------------------------------------------------------------

async def add_solved_problem(
    cf_handle: str,
    problem_key: str,
    rating: int | None,
    points: int,
    solved_at: str,
) -> bool:
    """
    Insert a solved problem record and update the user's total_points.

    Returns
    -------
    True  — newly inserted (points credited)
    False — duplicate; nothing changed
    """
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                """
                INSERT INTO solved_problems (cf_handle, problem_key, rating, points_awarded, solved_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (cf_handle, problem_key, rating, points, solved_at),
            )
            await db.execute(
                "UPDATE users SET total_points = total_points + ? WHERE cf_handle = ?",
                (points, cf_handle),
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False  # UNIQUE constraint → duplicate


async def get_all_solved_keys(cf_handle: str) -> set[str]:
    """Return the set of all problem_keys already stored for this handle."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT problem_key FROM solved_problems WHERE cf_handle = ?", (cf_handle,)
        ) as cur:
            rows = await cur.fetchall()
            return {row[0] for row in rows}


async def get_all_linked_users() -> list[dict[str, Any]]:
    """Return every row in the users table (discord_id + cf_handle)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT discord_id, cf_handle FROM users") as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Leaderboard
# ---------------------------------------------------------------------------

async def get_leaderboard(limit: int = 15) -> list[dict[str, Any]]:
    """
    Return top `limit` users ordered by total_points descending.
    Each row: discord_id, cf_handle, total_points, solved_count.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
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
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Daily leaderboard
# ---------------------------------------------------------------------------

async def get_daily_leaderboard(limit: int = 10) -> list[dict[str, Any]]:
    """
    Return top ``limit`` users by **points earned in the last 24 hours**.

    Sort order
    ----------
    1. ``daily_points`` DESC   (primary)
    2. ``daily_rating_sum`` DESC  (tie-breaker — sum of CF ratings solved today)

    Each row contains
    -----------------
    discord_id, cf_handle, daily_points, daily_solved, daily_rating_sum,
    top_problem_key (highest-rated problem today), top_problem_rating.

    Implementation note on ``solved_at`` comparison
    ------------------------------------------------
    Timestamps are stored as ISO-8601 strings with a '+00:00' suffix
    (e.g. '2026-11-05T17:04:21+00:00').  SQLite's ``datetime()`` does not
    parse timezone offsets in input strings, so we normalise via
    ``REPLACE(SUBSTR(solved_at,1,19),'T',' ')`` before comparing with
    ``datetime('now','-24 hours')``.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
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
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


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
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Basic row
        async with db.execute(
            "SELECT * FROM users WHERE cf_handle = ?", (cf_handle,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        stats: dict[str, Any] = dict(row)

        # Unique solved count
        async with db.execute(
            "SELECT COUNT(*) FROM solved_problems WHERE cf_handle = ?", (cf_handle,)
        ) as cur:
            stats["solved_count"] = (await cur.fetchone())[0]

        # Rating distribution bucketed by CF difficulty tiers
        async with db.execute(
            """
            SELECT
                CASE
                    WHEN rating IS NULL              THEN 'Unrated'
                    WHEN rating < 800                THEN '<800'
                    WHEN rating BETWEEN 800  AND 999 THEN '800–999'
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
            (cf_handle,),
        ) as cur:
            dist_rows = await cur.fetchall()
            stats["rating_distribution"] = {r[0]: r[1] for r in dist_rows}

        return stats

