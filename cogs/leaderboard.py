"""
cogs/leaderboard.py — /leaderboard slash command.

Shows top-15 server members ranked by total_points.
Each row now displays the user's current rank badge (S1–C3 or Unranked).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import db
from roles import tier_badge

log = logging.getLogger("cf-bot.leaderboard")

EMBED_COLOR  = 0x3B82F6
RANK_MEDALS  = {1: "🥇", 2: "🥈", 3: "🥉"}


class LeaderboardCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="leaderboard",
        description="Show the server's Codeforces leaderboard (top 15).",
    )
    async def leaderboard(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()

        rows = await db.get_leaderboard(limit=15)

        embed = discord.Embed(
            title="🏆 Codeforces Leaderboard",
            color=EMBED_COLOR,
            timestamp=datetime.now(timezone.utc),
        )

        if not rows:
            embed.description = (
                "No users are linked yet!\n"
                "Use `/link <cf_handle>` to register and start earning points."
            )
            await interaction.followup.send(embed=embed)
            return

        lines: list[str] = []
        for i, row in enumerate(rows, start=1):
            pos_str    = RANK_MEDALS.get(i, f"`#{i:>2}`")
            discord_id = row["discord_id"]
            cf_handle  = row["cf_handle"]
            pts        = row["total_points"]
            solved     = row["solved_count"]
            badge      = tier_badge(pts)

            # Prefer a friendly display name from the guild cache
            member = (
                interaction.guild.get_member(int(discord_id))
                if interaction.guild else None
            )
            if member:
                display = discord.utils.escape_markdown(member.display_name)
            else:
                display = f"<@{discord_id}>"

            lines.append(
                f"{pos_str} {badge}  **{display}**  •  "
                f"[{cf_handle}](https://codeforces.com/profile/{cf_handle})\n"
                f"┗ ✅ {solved} solved  •  ⭐ **{pts:,} pts**"
            )

        embed.description = "\n\n".join(lines)
        embed.set_footer(text="Updates every ~3 min  •  Codeforces • Keep Coding!")

        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LeaderboardCog(bot))
