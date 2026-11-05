"""
cogs/stats.py — /stats [user] slash command.

Shows:
  • CF profile summary (rank, rating, max rating) — live from CF API
  • Server rank role + progress bar to next tier  — from local DB + roles.py
  • Leaderboard points & unique problems solved    — from local DB
  • Rating distribution breakdown                 — from local DB
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import cf_api
import db
from roles import get_next_tier, get_tier, rank_progress, tier_badge, TIERS

log = logging.getLogger("cf-bot.stats")

EMBED_COLOR = 0x3B82F6
ERROR_COLOR  = 0xEF4444

# Bucket order for the rating distribution display
_BUCKET_ORDER = [
    "Unrated", "<800", "800–999", "1000–1199", "1200–1399",
    "1400–1599", "1600–1799", "1800–1999", "2000–2399", "2400+",
]

# CF rank colour map
_RANK_COLORS: dict[str, int] = {
    "newbie":                    0x808080,
    "pupil":                     0x008000,
    "specialist":                0x03A89E,
    "expert":                    0x0000FF,
    "candidate master":          0xAA00AA,
    "master":                    0xFF8C00,
    "international master":      0xFF8C00,
    "grandmaster":               0xFF0000,
    "international grandmaster": 0xFF0000,
    "legendary grandmaster":     0xFF0000,
}


class StatsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="stats",
        description="Show Codeforces stats for yourself or another server member.",
    )
    @app_commands.describe(user="Whose stats to view (defaults to you)")
    async def stats(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
    ) -> None:
        await interaction.response.defer()

        target = user or interaction.user

        # ── 1. Check DB ───────────────────────────────────────────────
        db_user = await db.get_user_by_discord_id(str(target.id))
        if db_user is None:
            suffix = (
                "Use `/link <cf_handle>` to get started."
                if target == interaction.user
                else "They haven't linked a Codeforces account yet."
            )
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Not Linked",
                    description=f"{target.mention} has no linked account.\n{suffix}",
                    color=ERROR_COLOR,
                ),
                ephemeral=True,
            )
            return

        cf_handle = db_user["cf_handle"]

        # ── 2. Fetch DB stats + live CF profile concurrently ──────────
        stats_task = asyncio.ensure_future(db.get_user_stats(cf_handle))
        info_task  = asyncio.ensure_future(cf_api.get_user_info(cf_handle))
        stats, cf_info = await asyncio.gather(stats_task, info_task)

        total_pts: int = stats["total_points"]

        # ── 3. Colour: prefer CF rank colour, fall back to tier colour ─
        cf_rank_raw = (cf_info or {}).get("rank", "unrated")
        tier_info   = get_tier(total_pts)
        color = (
            _RANK_COLORS.get(cf_rank_raw.lower())
            or (tier_info[1] if tier_info else EMBED_COLOR)
        )

        embed = discord.Embed(
            title=f"📊 Stats — {discord.utils.escape_markdown(target.display_name)}",
            color=color,
            timestamp=datetime.now(timezone.utc),
        )

        # ── 4. CF profile block ───────────────────────────────────────
        if cf_info:
            cf_rank    = cf_rank_raw.title()
            cf_rating  = cf_info.get("rating", "—")
            cf_max     = cf_info.get("maxRating", "—")
            cf_country = cf_info.get("country", "")
            cf_org     = cf_info.get("organization", "")

            profile_lines = [
                f"**Handle**: [{cf_handle}](https://codeforces.com/profile/{cf_handle})",
                f"**CF Rank**: {cf_rank}",
                f"**Rating**: {cf_rating}  •  **Max**: {cf_max}",
            ]
            if cf_country:
                profile_lines.append(f"**Country**: {cf_country}")
            if cf_org:
                profile_lines.append(f"**Organisation**: {cf_org}")

            embed.description = "\n".join(profile_lines)

            title_photo = cf_info.get("titlePhoto", "")
            if title_photo:
                embed.set_thumbnail(
                    url=f"https:{title_photo}" if title_photo.startswith("//") else title_photo
                )
        else:
            embed.description = f"**Handle**: `{cf_handle}` *(live profile unavailable)*"

        # ── 5. Server rank role ───────────────────────────────────────
        badge = tier_badge(total_pts)
        embed.add_field(name="🏅 Server Rank",     value=badge,                     inline=True)
        embed.add_field(name="⭐ Total Points",     value=f"**{total_pts:,}** pts",  inline=True)
        embed.add_field(name="✅ Problems Solved",  value=f"**{stats['solved_count']}** unique", inline=True)

        # ── 6. Rank progress bar ──────────────────────────────────────
        next_t = get_next_tier(total_pts)
        progress_title = (
            f"⬆️ Progress to **{next_t[0]}**" if next_t else "⬆️ Rank Progress"
        )
        embed.add_field(
            name=progress_title,
            value=rank_progress(total_pts),
            inline=False,
        )

        # ── 7. Full tier table ────────────────────────────────────────
        tier_rows: list[str] = []
        for min_pts, name, _ in reversed(TIERS):   # S1 … C3
            from roles import TIER_BADGES
            icon    = TIER_BADGES.get(name, "⭐")
            reached = "✅" if total_pts >= min_pts else "🔒"
            tier_rows.append(f"{reached} {icon} **{name}** — {min_pts:,} pts")
        embed.add_field(name="📋 Rank Tiers", value="\n".join(tier_rows), inline=False)

        # ── 8. Rating distribution ────────────────────────────────────
        dist: dict[str, int] = stats.get("rating_distribution", {})
        if dist:
            dist_lines: list[str] = []
            for bucket in _BUCKET_ORDER:
                cnt = dist.get(bucket)
                if cnt:
                    bar = "█" * min(cnt, 20)
                    dist_lines.append(f"`{bucket:<10}` {bar} **{cnt}**")
            if dist_lines:
                embed.add_field(
                    name="📊 Rating Distribution",
                    value="\n".join(dist_lines),
                    inline=False,
                )

        # ── 9. Footer ─────────────────────────────────────────────────
        registered_at = db_user.get("registered_at", "")
        try:
            dt = datetime.fromisoformat(registered_at)
            footer_text = f"Linked {dt.strftime('%d %b %Y')}  •  Codeforces • Keep Coding!"
        except (ValueError, TypeError):
            footer_text = "Codeforces • Keep Coding!"
        embed.set_footer(text=footer_text)

        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(StatsCog(bot))
