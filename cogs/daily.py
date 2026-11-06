"""
cogs/daily.py — /dailyleaderboard slash command + automated midnight summary.

Daily leaderboard mechanics
────────────────────────────
• Looks back exactly 24 hours using the ``solved_at`` column in SQLite.
• No daily "reset" is needed — the query window slides forward automatically.
• All-time ``total_points`` (used by /leaderboard) are unaffected.

Ranking
───────
1. Daily bot points   (SUM of points_awarded in last 24 h)
2. Sum of CF ratings  (tie-breaker: higher Σ-rating wins)

Auto-announcement
─────────────────
• Fires daily at the UTC time derived from DAILY_UTC_HOUR / DAILY_UTC_MINUTE.
• Default: 18:30 UTC  =  00:00 IST  (UTC+5:30).
• Pings the @cplovers role (CPLOVERS_ROLE_ID) and crowns the Coder of the Day.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, time, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

import db
from roles import tier_badge

log = logging.getLogger("cf-bot.daily")

# ---------------------------------------------------------------------------
# Config (read once at import time, after load_dotenv() in main.py)
# ---------------------------------------------------------------------------

CPLOVERS_ROLE_ID: int = int(os.getenv("CPLOVERS_ROLE_ID", "0"))
_UTC_HOUR:        int = int(os.getenv("DAILY_UTC_HOUR",   "18"))
_UTC_MIN:         int = int(os.getenv("DAILY_UTC_MINUTE", "30"))

# discord.ext.tasks uses this time to schedule the daily loop
_MIDNIGHT_UTC = time(hour=_UTC_HOUR, minute=_UTC_MIN, second=0, tzinfo=timezone.utc)

DAILY_COLOR  = 0xF97316   # Orange — "fire" theme for daily leaderboard
RANK_MEDALS  = {1: "🥇", 2: "🥈", 3: "🥉"}

# Regex: split a CF problem key like "213A" or "1234C1" into (contestId, index)
_KEY_RE = re.compile(r"^(\d+)([A-Za-z]\d*)$")


def _problem_link(problem_key: str) -> str:
    """Return a markdown hyperlink for a CF problem key, or the key as plain text."""
    m = _KEY_RE.match(problem_key or "")
    if not m:
        return problem_key or "—"
    contest_id, index = m.group(1), m.group(2).upper()
    url = f"https://codeforces.com/problemset/problem/{contest_id}/{index}"
    return f"[{problem_key}]({url})"


class DailyCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.midnight_summary.start()

    def cog_unload(self) -> None:
        self.midnight_summary.cancel()

    # ------------------------------------------------------------------
    # /dailyleaderboard  — on-demand
    # ------------------------------------------------------------------

    @app_commands.command(
        name="dailyleaderboard",
        description="Show today's top Codeforces solvers (last 24 hours).",
    )
    async def dailyleaderboard(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()

        rows = await db.get_daily_leaderboard(limit=10)
        embed = self._build_embed(
            rows,
            title="🔥 Today's Codeforces Leaderboard",
            description="Top solvers in the **last 24 hours**, ranked by daily points.",
        )
        await interaction.followup.send(embed=embed)

    # ------------------------------------------------------------------
    # Daily midnight summary  — automated
    # ------------------------------------------------------------------

    @tasks.loop(time=_MIDNIGHT_UTC)
    async def midnight_summary(self) -> None:
        channel = self._get_coding_channel()
        if channel is None:
            log.warning("Midnight summary: CODING_CHANNEL_ID not set or channel not found.")
            return

        rows = await db.get_daily_leaderboard(limit=10)

        # Role ping
        role_mention = f"<@&{CPLOVERS_ROLE_ID}> " if CPLOVERS_ROLE_ID else ""

        # Coder of the Day block
        if rows:
            winner = rows[0]
            coder_line = (
                f"🏆 **Coder of the Day**: <@{winner['discord_id']}> "
                f"(`{winner['cf_handle']}`) — "
                f"**{winner['daily_points']} pts** across "
                f"**{winner['daily_solved']}** problem(s)! 🎉"
            )
        else:
            coder_line = "No problems solved today — let's grind harder tomorrow! 💪"

        content = (
            f"{role_mention}📊 **Daily Standings are in!**\n{coder_line}"
        )

        embed = self._build_embed(
            rows,
            title="🏆 Daily Codeforces Standings Summary",
            description=(
                "Here's how everyone performed in the **last 24 hours**.\n"
                "New day, fresh grind — good luck! 🚀"
            ),
        )
        embed.timestamp = datetime.now(timezone.utc)

        try:
            await channel.send(content=content, embed=embed)
            log.info(
                "Midnight summary posted to #%s (%d entries).",
                channel.name, len(rows),
            )
        except discord.Forbidden:
            log.error("Missing permission to post in channel %s.", channel.id)
        except discord.HTTPException as exc:
            log.warning("Failed to post midnight summary: %s", exc)

    @midnight_summary.before_loop
    async def _before_midnight(self) -> None:
        await self.bot.wait_until_ready()
        log.info(
            "Midnight summary scheduled for %02d:%02d UTC (%02d:%02d IST-equivalent) daily.",
            _UTC_HOUR, _UTC_MIN,
            (_UTC_HOUR + 5) % 24, (_UTC_MIN + 30) % 60,
        )

    # ------------------------------------------------------------------
    # Shared embed builder
    # ------------------------------------------------------------------

    def _build_embed(
        self,
        rows:        list[dict],
        title:       str,
        description: str,
    ) -> discord.Embed:
        embed = discord.Embed(title=title, color=DAILY_COLOR)

        if not rows:
            embed.description = (
                f"{description}\n\n"
                "⚠️ No problems solved in the last 24 hours.\n"
                "Get solving — every problem counts! 💪"
            )
            embed.set_footer(text="Last 24 hours  •  Codeforces • Keep Coding!")
            return embed

        lines: list[str] = []
        for i, row in enumerate(rows, start=1):
            pos_str    = RANK_MEDALS.get(i, f"`#{i:>2}`")
            discord_id = row["discord_id"]
            cf_handle  = row["cf_handle"]
            pts        = row["daily_points"]
            solved     = row["daily_solved"]
            rating_sum = row.get("daily_rating_sum") or 0
            top_key    = row.get("top_problem_key") or ""
            top_rating = row.get("top_problem_rating")

            # Friendly display name from any guild cache
            display = f"<@{discord_id}>"
            for guild in self.bot.guilds:
                member = guild.get_member(int(discord_id))
                if member:
                    display = discord.utils.escape_markdown(member.display_name)
                    break

            # Top problem
            top_link = _problem_link(top_key)
            if top_rating:
                top_link += f" `{top_rating}`"

            # Tie-breaker visibility: rating sum
            rating_sum_str = f"{rating_sum:,}" if rating_sum else "0"

            lines.append(
                f"{pos_str} **{display}** · `{cf_handle}`\n"
                f"┣ ⭐ **{pts} pts**  ·  ✅ {solved} solved  ·  Σ rating `{rating_sum_str}`\n"
                f"┗ 🔝 Top problem: {top_link}"
            )

        embed.description = f"{description}\n\n" + "\n\n".join(lines)
        embed.set_footer(text="Last 24 hours  •  Codeforces • Keep Coding!")
        return embed

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_coding_channel(self) -> discord.TextChannel | None:
        channel_id: int = getattr(self.bot, "coding_channel_id", 0)
        if not channel_id:
            return None
        return self.bot.get_channel(channel_id)  # type: ignore[return-value]


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DailyCog(bot))
