"""
cogs/tracker.py — Background submission polling + auto-announcements.

Poll loop (every 3 minutes)
────────────────────────────
For every linked user:
  1. Fetch their 50 most-recent CF submissions.
  2. Filter for new, unique accepted submissions (verdict == "OK") not yet
     in solved_problems.
  3. Award points and store in DB.
  4. Update the member's Discord rank role across all guilds.
  5. If a tier promotion occurred, include a promotion banner in the
     announcement embed sent to the designated #coding channel.

Rate-limit safety
─────────────────
• 1-second sleep between consecutive users during a poll cycle.
• The loop fires every 3 minutes → well within CF's ~5 req/s limit.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks

import cf_api
import db
from points import calculate_points
from roles import get_tier, tier_badge, update_user_roles

log = logging.getLogger("cf-bot.tracker")

_POLL_MINUTES  = 3
_SLEEP_BETWEEN = 1.0   # seconds between user polls
_RECENT_COUNT  = 50    # submissions to fetch per poll

SOLVE_COLOR     = 0x22C55E   # green  — regular solve
PROMOTE_COLOR   = 0xF59E0B   # amber  — promotion solve
CF_PROBLEM_BASE = "https://codeforces.com/problemset/problem/{contest_id}/{index}"


class TrackerCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.poll_loop.start()

    def cog_unload(self) -> None:
        self.poll_loop.cancel()

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    @tasks.loop(minutes=_POLL_MINUTES)
    async def poll_loop(self) -> None:
        users = await db.get_all_linked_users()
        if not users:
            return

        log.info("Poll cycle — checking %d user(s).", len(users))
        channel = self._get_coding_channel()

        for user in users:
            cf_handle  = user["cf_handle"]
            discord_id = user["discord_id"]
            try:
                await self._check_user(cf_handle, discord_id, channel)
            except Exception:
                log.exception("Unexpected error polling %s", cf_handle)
            await asyncio.sleep(_SLEEP_BETWEEN)

    @poll_loop.before_loop
    async def _before_poll(self) -> None:
        await self.bot.wait_until_ready()
        log.info("Tracker poll loop is ready (every %d min).", _POLL_MINUTES)

    # ------------------------------------------------------------------
    # Per-user poll
    # ------------------------------------------------------------------

    async def _check_user(
        self,
        cf_handle:  str,
        discord_id: str,
        channel:    discord.TextChannel | None,
    ) -> None:
        submissions = await cf_api.get_user_status(cf_handle, from_=1, count=_RECENT_COUNT)
        if not submissions:
            return

        stored_keys = await db.get_all_solved_keys(cf_handle)

        # Snapshot points BEFORE this batch so we can detect promotions
        user_row     = await db.get_user_by_cf_handle(cf_handle)
        running_pts  = user_row["total_points"] if user_row else 0

        # Collect new accepted solves; deduplicate within this batch
        seen_this_batch: set[str] = set()
        new_solves: list[dict]    = []

        for sub in submissions:
            if sub.get("verdict") != "OK":
                continue
            problem    = sub.get("problem", {})
            contest_id = problem.get("contestId")
            index      = problem.get("index", "")
            if not contest_id:
                continue   # gym / non-standard; skip

            key = f"{contest_id}{index}"
            if key in stored_keys or key in seen_this_batch:
                continue

            seen_this_batch.add(key)
            rating    = problem.get("rating")
            pts       = calculate_points(rating)
            solved_at = datetime.fromtimestamp(
                sub.get("creationTimeSeconds", 0), tz=timezone.utc
            ).isoformat()

            new_solves.append({
                "problem_key": key,
                "contest_id":  str(contest_id),
                "index":       index,
                "name":        problem.get("name", "Unknown Problem"),
                "rating":      rating,
                "points":      pts,
                "solved_at":   solved_at,
            })

        for solve in new_solves:
            inserted = await db.add_solved_problem(
                cf_handle,
                solve["problem_key"],
                solve["rating"],
                solve["points"],
                solve["solved_at"],
            )
            if not inserted:
                continue

            prev_pts     = running_pts
            running_pts += solve["points"]

            log.info(
                "New solve — %s: %s (+%d pts, total %d)",
                cf_handle, solve["problem_key"], solve["points"], running_pts,
            )

            # ── Role update across all guilds ─────────────────────────
            promotion: tuple[str | None, str | None] | None = None
            for guild in self.bot.guilds:
                member = guild.get_member(int(discord_id))
                if member is None:
                    continue
                old_t, new_t = await update_user_roles(guild, member, running_pts)
                # Record first detected promotion (should be the same in every guild)
                if promotion is None and old_t != new_t and new_t is not None:
                    promotion = (old_t, new_t)

            # ── Announcement ─────────────────────────────────────────
            if channel:
                await self._announce(
                    channel, discord_id, cf_handle, solve,
                    running_pts, promotion,
                )

    # ------------------------------------------------------------------
    # Announcement embed
    # ------------------------------------------------------------------

    async def _announce(
        self,
        channel:    discord.TextChannel,
        discord_id: str,
        cf_handle:  str,
        solve:      dict,
        total_pts:  int,
        promotion:  tuple[str | None, str | None] | None,
    ) -> None:
        url         = CF_PROBLEM_BASE.format(contest_id=solve["contest_id"], index=solve["index"])
        rating      = solve["rating"]
        rating_str  = f"`{rating}`" if rating is not None else "`Unrated`"
        pts         = solve["points"]
        problem_key = solve["problem_key"]
        name        = solve["name"]

        # Difficulty icon
        if rating is None:         diff_icon = "⚪"
        elif rating < 1200:        diff_icon = "🟢"
        elif rating < 1900:        diff_icon = "🔵"
        elif rating < 2400:        diff_icon = "🟡"
        else:                      diff_icon = "🔴"

        promoted      = promotion is not None
        embed_color   = PROMOTE_COLOR if promoted else SOLVE_COLOR
        embed_title   = "🏆 New Solve + Promotion!" if promoted else "🎉 New Problem Solved!"

        embed = discord.Embed(
            title=embed_title,
            color=embed_color,
            timestamp=datetime.fromisoformat(solve["solved_at"]),
        )
        embed.add_field(
            name="Problem",
            value=f"[{problem_key} — {name}]({url})",
            inline=False,
        )
        embed.add_field(name="Difficulty",    value=f"{diff_icon} {rating_str}", inline=True)
        embed.add_field(name="Points Earned", value=f"⭐ **+{pts}**",           inline=True)
        embed.add_field(name="Total Points",  value=f"**{total_pts:,}**",       inline=True)

        # Promotion banner
        if promoted:
            old_t, new_t = promotion
            old_label = f"**{old_t}**" if old_t else "**Unranked**"
            embed.add_field(
                name="🏅 Rank Promotion!",
                value=f"{old_label} → **{new_t}** {_tier_badge_str(new_t)}",
                inline=False,
            )

        embed.set_footer(text=f"{tier_badge(total_pts)}  •  Codeforces • Keep Coding!")

        # Compose message content
        content_lines = [
            f"🎉 <@{discord_id}> (`{cf_handle}`) just solved "
            f"**[{problem_key} — {name}]({url})** (Rating: {rating_str})!",
            f"➕ **+{pts} Points** added to the leaderboard.  Total: **{total_pts:,}**",
        ]
        if promoted:
            _, new_t = promotion
            content_lines.append(
                f"🚀 **<@{discord_id}> has been promoted to rank {new_t}!** 🎊"
            )
        content_lines.append(f"🔗 {url}")

        try:
            await channel.send(content="\n".join(content_lines), embed=embed)
        except discord.Forbidden:
            log.error("Missing permission to send in channel %s", channel.id)
        except discord.HTTPException as exc:
            log.warning("Failed to send announcement: %s", exc)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_coding_channel(self) -> discord.TextChannel | None:
        channel_id: int = getattr(self.bot, "coding_channel_id", 0)
        if not channel_id:
            return None
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            log.warning(
                "CODING_CHANNEL_ID %d not found in cache. "
                "Make sure the bot has access to that channel.",
                channel_id,
            )
        return channel  # type: ignore[return-value]


def _tier_badge_str(tier_name: str | None) -> str:
    """Minimal badge emoji for use inside embed field values."""
    from roles import TIER_BADGES
    return TIER_BADGES.get(tier_name or "", "⭐")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TrackerCog(bot))
