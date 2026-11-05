"""
roles.py — Rank tier definitions and Discord role management.

Tier table (highest threshold first)
─────────────────────────────────────
 10 000+  → C3  🔴
  5 000+  → C2  🟠
  2 000+  → C1  🟣
  1 000+  → S3  🔵
    500+  → S2  🩵
    100+  → S1  🟢
      0   → Unranked  ⬛

Public API
──────────
  get_tier(points)        → (name, color) | None
  get_next_tier(points)   → (name, color, min_pts) | None
  tier_badge(points)      → str   e.g. "🔵 S3"
  rank_progress(points)   → str   progress-bar string
  update_user_roles(...)  → (old_tier_name, new_tier_name)
"""

from __future__ import annotations

import logging

import discord

log = logging.getLogger("cf-bot.roles")

# ---------------------------------------------------------------------------
# Tier constants
# ---------------------------------------------------------------------------

# (min_points, role_name, discord_color_int)  — ordered highest-first
TIERS: list[tuple[int, str, int]] = [
    (10_000, "C3", 0xEF4444),  # Red
    ( 5_000, "C2", 0xF59E0B),  # Amber / Gold
    ( 2_000, "C1", 0x8B5CF6),  # Violet
    ( 1_000, "S3", 0x3B82F6),  # Blue
    (   500, "S2", 0x06B6D4),  # Cyan
    (   100, "S1", 0x22C55E),  # Green
]

ALL_TIER_NAMES: frozenset[str] = frozenset(name for _, name, _ in TIERS)

TIER_BADGES: dict[str, str] = {
    "C3": "🔴",
    "C2": "🟠",
    "C1": "🟣",
    "S3": "🔵",
    "S2": "🩵",
    "S1": "🟢",
}

# Reverse-lookup: role_name → position index (0 = highest tier = C3)
_TIER_INDEX: dict[str, int] = {name: i for i, (_, name, _) in enumerate(TIERS)}


# ---------------------------------------------------------------------------
# Pure helpers (no Discord I/O)
# ---------------------------------------------------------------------------


def get_tier(points: int) -> tuple[str, int] | None:
    """
    Return ``(role_name, color)`` for the highest tier the user qualifies for,
    or ``None`` if they're unranked (< 100 points).
    """
    for min_pts, name, color in TIERS:   # highest-first → first match wins
        if points >= min_pts:
            return name, color
    return None


def get_next_tier(points: int) -> tuple[str, int, int] | None:
    """
    Return ``(role_name, color, min_pts)`` for the tier directly above the
    user's current one, or ``None`` if they're already at C3.
    """
    for min_pts, name, color in reversed(TIERS):   # lowest-first
        if points < min_pts:
            return name, color, min_pts
    return None   # already at C3


def tier_badge(points: int) -> str:
    """Short badge string suitable for the leaderboard embed, e.g. ``"🔵 S3"``."""
    t = get_tier(points)
    if t is None:
        return "⬛ Unranked"
    name, _ = t
    return f"{TIER_BADGES.get(name, '⭐')} {name}"


def rank_progress(points: int) -> str:
    """
    Return a progress-bar string toward the next tier.

    Example outputs
    ---------------
    ``"████████████░░░░░░░░ 750/1,000 pts → S3 (75%)"``
    ``"🏆 Max rank — **C3** achieved!"``
    """
    next_t = get_next_tier(points)
    if next_t is None:
        return "🏆 **Max rank — C3 achieved!**"

    next_name, _, next_min = next_t
    pct    = min(points / next_min, 1.0)
    filled = round(pct * 20)
    bar    = "█" * filled + "░" * (20 - filled)
    return (
        f"`{bar}` **{points:,} / {next_min:,}** pts → **{next_name}** ({pct:.0%})"
    )


# ---------------------------------------------------------------------------
# Discord role management
# ---------------------------------------------------------------------------


async def update_user_roles(
    guild:      discord.Guild,
    member:     discord.Member,
    new_points: int,
) -> tuple[str | None, str | None]:
    """
    Assign the correct tier role and strip stale tier roles for *member*.

    Parameters
    ----------
    guild      : Guild to operate in.
    member     : The member to update.
    new_points : The member's authoritative total points after the latest solve.

    Returns
    -------
    ``(old_tier_name, new_tier_name)``
        Either value can be ``None`` (unranked or permission failure).
        A **promotion** occurred when ``new_tier_name != old_tier_name``
        and ``new_tier_name is not None``.

    Notes
    -----
    * If the target role doesn't exist on the server it is **created automatically**
      with the tier's colour and ``hoist=True``.
    * All errors (Forbidden, HTTP) are caught and logged — the function never raises.
    * The bot's role **must be positioned above** all rank roles in
      Server Settings → Roles, otherwise Discord will reject the assignment.
    """
    # ── Determine target tier ─────────────────────────────────────────────
    tier_info   = get_tier(new_points)
    target_name: str | None = tier_info[0] if tier_info else None
    target_color: int       = tier_info[1] if tier_info else 0

    # ── Identify existing tier roles the member holds ─────────────────────
    current_tier_roles: list[discord.Role] = [
        r for r in member.roles if r.name in ALL_TIER_NAMES
    ]

    # Highest-held tier = the one with the smallest index in TIERS (most prestige)
    old_tier_name: str | None = (
        min(current_tier_roles, key=lambda r: _TIER_INDEX.get(r.name, 999)).name
        if current_tier_roles else None
    )

    # Short-circuit: nothing to change
    if old_tier_name == target_name:
        return old_tier_name, target_name

    # ── Find or auto-create the target Discord role ───────────────────────
    target_role: discord.Role | None = None
    if target_name:
        target_role = discord.utils.get(guild.roles, name=target_name)
        if target_role is None:
            try:
                target_role = await guild.create_role(
                    name=target_name,
                    color=discord.Color(target_color),
                    hoist=True,          # separate section in member list
                    mentionable=False,
                    reason="CF Bot: auto-created rank role",
                )
                log.info("Created role '%s' in guild '%s'.", target_name, guild.name)
            except discord.Forbidden:
                log.error(
                    "Missing 'Manage Roles' permission to create '%s' in '%s'. "
                    "Move the bot's role ABOVE all rank roles in Server Settings → Roles.",
                    target_name, guild.name,
                )
                return old_tier_name, None
            except discord.HTTPException as exc:
                log.warning("Failed to create role '%s': %s", target_name, exc)
                return old_tier_name, None

    # ── Apply changes ─────────────────────────────────────────────────────
    roles_to_remove = [r for r in current_tier_roles if r.name != target_name]
    try:
        if roles_to_remove:
            await member.remove_roles(*roles_to_remove, reason="CF Bot: rank update")
        if target_role and target_role not in member.roles:
            await member.add_roles(target_role, reason="CF Bot: rank promotion")
    except discord.Forbidden:
        log.error(
            "Missing permission to modify roles for '%s' in '%s'. "
            "Ensure the bot's role is positioned ABOVE all CF rank roles.",
            member.display_name, guild.name,
        )
        return old_tier_name, None
    except discord.HTTPException as exc:
        log.warning("Failed to update roles for '%s': %s", member.display_name, exc)
        return old_tier_name, None

    log.info(
        "Rank update  %-20s  %s → %s  (%d pts)  [%s]",
        member.display_name,
        old_tier_name or "Unranked",
        target_name   or "Unranked",
        new_points,
        guild.name,
    )
    return old_tier_name, target_name

