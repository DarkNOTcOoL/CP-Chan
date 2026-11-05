"""
cogs/link.py — /link <cf_handle> slash command.

Flow
----
1. Validate the handle exists via CF user.info API.
2. Persist the mapping in `users` table.
3. Kick off a background backfill task that:
   - Fetches the full accepted-submission history (paginated).
   - Deduplicates by problem_key.
   - Inserts each unique accepted problem into solved_problems.
   - Updates total_points accordingly.
4. Assigns the correct Discord rank role based on the final points total.
5. Sends an ephemeral completion message once backfill finishes.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

import cf_api
import db
from points import calculate_points
from roles import get_tier, rank_progress, tier_badge, update_user_roles

log = logging.getLogger("cf-bot.link")

EMBED_COLOR   = 0x3B82F6
ERROR_COLOR   = 0xEF4444
SUCCESS_COLOR = 0x22C55E


def _err(title: str, desc: str) -> discord.Embed:
    return discord.Embed(title=f"❌ {title}", description=desc, color=ERROR_COLOR)


class LinkCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # /link
    # ------------------------------------------------------------------

    @app_commands.command(
        name="link",
        description="Link your Discord account to your Codeforces handle.",
    )
    @app_commands.describe(cf_handle="Your Codeforces username (handle)")
    async def link(self, interaction: discord.Interaction, cf_handle: str) -> None:
        await interaction.response.defer(ephemeral=True)

        # ── 1. Guard: already linked to the same handle ──────────────
        existing = await db.get_user_by_discord_id(str(interaction.user.id))
        if existing and existing["cf_handle"].lower() == cf_handle.lower():
            await interaction.followup.send(
                embed=_err(
                    "Already Linked",
                    f"You're already linked to `{existing['cf_handle']}`.\n"
                    "Use `/link <new_handle>` to switch handles.",
                ),
                ephemeral=True,
            )
            return

        # ── 2. Validate handle with CF API ────────────────────────────
        user_info = await cf_api.get_user_info(cf_handle)
        if user_info is None:
            await interaction.followup.send(
                embed=_err(
                    "Invalid Handle",
                    f"No Codeforces user found for `{cf_handle}`.\n"
                    "Check the spelling and try again.",
                ),
                ephemeral=True,
            )
            return

        # Use the canonical handle returned by CF (preserves correct casing)
        canonical = user_info.get("handle", cf_handle)

        # ── 3. Guard: handle already claimed by someone else ──────────
        owner = await db.get_user_by_cf_handle(canonical)
        if owner and owner["discord_id"] != str(interaction.user.id):
            await interaction.followup.send(
                embed=_err(
                    "Handle Taken",
                    f"`{canonical}` is already linked to another Discord user.\n"
                    "If this is a mistake, contact a server admin.",
                ),
                ephemeral=True,
            )
            return

        # ── 4. Persist ────────────────────────────────────────────────
        await db.link_user(str(interaction.user.id), canonical)

        cf_rank   = user_info.get("rank", "unrated").title()
        cf_rating = user_info.get("rating", "N/A")

        await interaction.followup.send(
            embed=discord.Embed(
                title="✅ Account Linked!",
                description=(
                    f"Successfully linked to **[{canonical}](https://codeforces.com/profile/{canonical})**.\n\n"
                    f"**Current rank**: {cf_rank} (`{cf_rating}`)\n\n"
                    f"⏳ Backfilling your submission history in the background…\n"
                    f"You'll receive a follow-up message when it's done."
                ),
                color=SUCCESS_COLOR,
            ).set_footer(text="Codeforces • Keep Coding!"),
            ephemeral=True,
        )

        # ── 5. Backfill in the background ─────────────────────────────
        asyncio.ensure_future(
            self._backfill(interaction, canonical, user_info),
            loop=self.bot.loop,
        )

    # ------------------------------------------------------------------
    # Backfill helper
    # ------------------------------------------------------------------

    async def _backfill(
        self,
        interaction: discord.Interaction,
        cf_handle:   str,
        user_info:   dict[str, Any],
    ) -> None:
        log.info("Starting backfill for %s", cf_handle)
        try:
            accepted = await cf_api.get_all_accepted(cf_handle)

            # Deduplicate within the fetched batch — keep earliest per problem
            seen:   set[str]                        = set()
            unique: list[tuple[dict, str, dict]]    = []
            for sub in reversed(accepted):   # oldest-first → keep oldest occurrence
                problem    = sub.get("problem", {})
                contest_id = problem.get("contestId")
                index      = problem.get("index", "")
                if not contest_id:
                    continue
                key = f"{contest_id}{index}"
                if key not in seen:
                    seen.add(key)
                    unique.append((sub, key, problem))

            total_pts = 0
            inserted  = 0

            for sub, key, problem in unique:
                rating    = problem.get("rating")
                pts       = calculate_points(rating)
                solved_at = datetime.fromtimestamp(
                    sub.get("creationTimeSeconds", 0), tz=timezone.utc
                ).isoformat()
                ok = await db.add_solved_problem(cf_handle, key, rating, pts, solved_at)
                if ok:
                    inserted  += 1
                    total_pts += pts

            log.info(
                "Backfill done for %s — %d unique problems, %d pts",
                cf_handle, inserted, total_pts,
            )

            # ── Assign Discord rank role in the interaction's guild ───
            role_line = ""
            guild = interaction.guild
            if guild:
                # Fetch the actual DB total (more accurate than accumulated total_pts
                # in case of re-links or concurrent writes)
                user_row   = await db.get_user_by_cf_handle(cf_handle)
                final_pts  = user_row["total_points"] if user_row else total_pts

                member = guild.get_member(interaction.user.id)
                if member is None:
                    try:
                        member = await guild.fetch_member(interaction.user.id)
                    except discord.HTTPException:
                        member = None

                if member:
                    _, new_tier = await update_user_roles(guild, member, final_pts)
                    if new_tier:
                        role_line = f"\n🏅 **Rank assigned**: {tier_badge(final_pts)}"
                    else:
                        role_line = f"\n_(< 100 pts — no rank role yet)_"

            # ── Send completion follow-up ─────────────────────────────
            try:
                user_row  = user_row if guild else await db.get_user_by_cf_handle(cf_handle)
                final_pts = (user_row["total_points"] if user_row else total_pts) if not guild else final_pts

                embed = discord.Embed(
                    title="📦 Backfill Complete!",
                    description=(
                        f"Imported **{inserted}** unique solved problem{'s' if inserted != 1 else ''}.\n"
                        f"**+{total_pts:,}** starting points added to your leaderboard score.\n\n"
                        f"**CF Rank**: {user_info.get('rank', 'unrated').title()}\n"
                        f"**Max Rating**: {user_info.get('maxRating', 'N/A')}"
                        f"{role_line}\n\n"
                        f"**Progress to next tier:**\n{rank_progress(final_pts)}"
                    ),
                    color=0x22C55E,
                ).set_footer(text="Codeforces • Keep Coding!")

                await interaction.followup.send(embed=embed, ephemeral=True)
            except discord.NotFound:
                log.debug(
                    "Interaction token expired before backfill completion for %s",
                    cf_handle,
                )

        except Exception:
            log.exception("Backfill failed for %s", cf_handle)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LinkCog(bot))
