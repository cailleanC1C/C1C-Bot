"""Emergency recruitment open-spots correction command."""

from __future__ import annotations

import logging
import re
from typing import Optional

from discord.ext import commands

from c1c_coreops.helpers import help_metadata, tier
from c1c_coreops.rbac import is_admin_member, is_staff_member, ops_only
from modules.common import runtime as runtime_helpers
from modules.recruitment import availability

log = logging.getLogger(__name__)

USAGE = "!setopenspots <clan_tag_or_name> <open_spots> <reason>"


def _display_name(author: object) -> str:
    return str(
        getattr(author, "display_name", None)
        or getattr(author, "name", None)
        or getattr(author, "id", "Unknown")
    )


def _parse_whole_number(value: str | None) -> Optional[int]:
    if value is None or not re.fullmatch(r"\d+", str(value).strip()):
        return None
    parsed = int(str(value).strip())
    return parsed if parsed >= 0 else None


class RecruitmentOpenSpotsCog(commands.Cog):
    """Staff/admin emergency overrides for recruitment availability."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @tier("staff")
    @help_metadata(
        function_group="recruitment", section="recruitment", access_tier="staff"
    )
    @commands.command(
        name="setopenspots",
        usage="<clan_tag_or_name> <open_spots> <reason>",
        help="Emergency staff/admin correction for configured clan open spots.",
        brief="Corrects configured clan open spots.",
    )
    @ops_only()
    async def setopenspots(
        self,
        ctx: commands.Context,
        clan_tag_or_name: Optional[str] = None,
        open_spots: Optional[str] = None,
        *,
        reason: Optional[str] = None,
    ) -> None:
        """Manually correct a clan's configured open-spots availability value."""

        if not (is_staff_member(ctx) or is_admin_member(ctx)):
            await ctx.reply(
                "You do not have permission to use this command.", mention_author=False
            )
            return

        if (
            not clan_tag_or_name
            or open_spots is None
            or not reason
            or not reason.strip()
        ):
            await ctx.reply(f"Usage: {USAGE}", mention_author=False)
            return

        new_value = _parse_whole_number(open_spots)
        if new_value is None:
            await ctx.reply(
                "open_spots must be a whole number >= 0.", mention_author=False
            )
            return

        try:
            old_value, corrected_value, resolved_clan = (
                await availability.set_manual_open_spots(clan_tag_or_name, new_value)
            )
        except ValueError as exc:
            message = str(exc).lower()
            if "ambiguous" in message or "multiple clans" in message:
                await ctx.reply(
                    "That clan input matches multiple clans; please use the clan tag.",
                    mention_author=False,
                )
                return
            if "unknown clan" in message or "row_not_found" in message:
                await ctx.reply("The clan could not be found.", mention_author=False)
                return
            log.exception("setopenspots configuration/header resolution failed")
            await runtime_helpers.send_log_message(
                f"⚠️ Open spots correction failed: recruitment sheet configuration invalid for {clan_tag_or_name}."
            )
            await ctx.reply(
                "The correction could not be applied because recruitment sheet configuration is incomplete or invalid.",
                mention_author=False,
            )
            return
        except Exception:
            log.exception("setopenspots sheet update failed")
            await runtime_helpers.send_log_message(
                f"⚠️ Open spots correction failed: recruitment sheet configuration invalid for {clan_tag_or_name}."
            )
            await ctx.reply(
                "The correction could not be applied because recruitment sheet configuration is incomplete or invalid.",
                mention_author=False,
            )
            return

        actor = _display_name(ctx.author)
        actor_id = getattr(ctx.author, "id", None)
        await ctx.reply(
            "**Open spots corrected**\n\n"
            f"Clan: {resolved_clan}\n"
            f"Open spots: {old_value} → {corrected_value}\n"
            f"Reason: {reason.strip()}\n"
            f"Updated by: {actor}",
            mention_author=False,
        )
        id_suffix = f" ({actor_id})" if actor_id is not None else ""
        await runtime_helpers.send_log_message(
            f"🛠️ Open spots corrected: {resolved_clan} {old_value} → {corrected_value} by {actor}{id_suffix}\n"
            f"Reason: {reason.strip()}"
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RecruitmentOpenSpotsCog(bot))
