"""
cf_api.py — Async Codeforces API client.

A single aiohttp.ClientSession is reused across calls for connection pooling.
All public functions return Python dicts/lists or None on failure — callers
never see raw HTTP errors.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

log = logging.getLogger("cf-bot.api")

CF_BASE = "https://codeforces.com/api"
_SESSION: aiohttp.ClientSession | None = None

# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------


async def get_session() -> aiohttp.ClientSession:
    global _SESSION
    if _SESSION is None or _SESSION.closed:
        connector = aiohttp.TCPConnector(limit=10)
        _SESSION = aiohttp.ClientSession(connector=connector)
    return _SESSION


async def close_session() -> None:
    global _SESSION
    if _SESSION and not _SESSION.closed:
        await _SESSION.close()
        _SESSION = None


# ---------------------------------------------------------------------------
# Low-level GET
# ---------------------------------------------------------------------------


async def _get(
    endpoint: str,
    params: dict[str, Any],
    timeout: float = 15.0,
) -> Any | None:
    """
    Issue a GET request to the CF API and return the `result` field.
    Returns None on any error (network, non-OK status, CF API error).
    """
    session = await get_session()
    url = f"{CF_BASE}/{endpoint}"
    try:
        async with session.get(
            url, params=params, timeout=aiohttp.ClientTimeout(total=timeout)
        ) as resp:
            if resp.status != 200:
                log.warning("CF API %s → HTTP %s", endpoint, resp.status)
                return None
            data: dict = await resp.json(content_type=None)
    except asyncio.TimeoutError:
        log.warning("CF API %s timed out after %.0fs", endpoint, timeout)
        return None
    except aiohttp.ClientError as exc:
        log.warning("CF API %s client error: %s", endpoint, exc)
        return None

    if data.get("status") != "OK":
        log.warning("CF API %s returned error: %s", endpoint, data.get("comment"))
        return None

    return data.get("result")


# ---------------------------------------------------------------------------
# Public API helpers
# ---------------------------------------------------------------------------


async def get_user_info(handle: str) -> dict[str, Any] | None:
    """
    Validate a CF handle and return the user info dict.
    Returns None if the handle doesn't exist or the API is unreachable.
    """
    result = await _get("user.info", {"handles": handle})
    if isinstance(result, list) and result:
        return result[0]
    return None


async def get_user_status(
    handle: str,
    from_: int = 1,
    count: int = 50,
) -> list[dict[str, Any]]:
    """
    Return the most recent `count` submissions for `handle`.
    Safe to call in the polling loop — returns [] on failure.
    """
    result = await _get(
        "user.status",
        {"handle": handle, "from": from_, "count": count},
    )
    return result if isinstance(result, list) else []


async def get_all_accepted(handle: str) -> list[dict[str, Any]]:
    """
    Paginate through the full submission history for `handle` and return
    only accepted (verdict == "OK") submissions.

    Used for the initial backfill on /link.
    Sleeps 1 s between pages to respect the CF rate limit.
    """
    accepted: list[dict] = []
    from_ = 1
    page_size = 1_000

    while True:
        batch = await _get(
            "user.status",
            {"handle": handle, "from": from_, "count": page_size},
        )
        if not isinstance(batch, list) or not batch:
            break

        accepted.extend(s for s in batch if s.get("verdict") == "OK")

        if len(batch) < page_size:
            break  # last page

        from_ += page_size
        await asyncio.sleep(1.0)  # CF rate-limit safety

    return accepted

