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
CONFIG_FAILURE_MESSAGE = "The correction could not be applied because recruitment sheet configuration is incomplete or invalid."
CLAN_NOT_FOUND_MESSAGE = "The clan could not be found."
QUOTA_FAILURE_MESSAGE = "Google Sheets quota/rate limits are temporarily exhausted, so the correction was not applied. Please wait a few minutes and try again."
ROW_CONFIG_FAILURE_MESSAGE = "The correction could not be applied because the clan row or recruitment sheet configuration could not be resolved."
WRITE_FAILURE_MESSAGE = "The correction could not be applied because the sheet write failed. Please try again or contact an admin."
VERIFY_FAILURE_MESSAGE = "The correction may have written to Sheets, but post-write verification failed. Please check the clan row before retrying."


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


def _is_clan_not_found_error(exc: ValueError) -> bool:
    message = str(exc).lower()
    return "unknown clan" in message or "row_not_found" in message


def _is_ambiguous_clan_error(exc: ValueError) -> bool:
    message = str(exc).lower()
    return "ambiguous" in message or "multiple clans" in message


def _is_config_or_preflight_error(exc: ValueError) -> bool:
    message = str(exc).lower()
    config_markers = (
        "missing required config key",
        "configured bot_info header not found",
        "worksheet not accessible",
        "non_numeric_",
    )
    return any(marker in message for marker in config_markers)


def _caller_source(ctx: commands.Context) -> str:
    if is_admin_member(ctx):
        return "admin"
    if is_staff_member(ctx):
        return "staff"
    return "unknown"


def _operation_phase(exc: BaseException) -> str:
    phase = str(getattr(exc, "phase", "") or "").strip().lower().replace("_", "-")
    if phase in {
        "preflight",
        "worksheet-lookup",
        "worksheet-update",
        "cache-refresh",
        "post-write verification",
        "post-write-verification",
    }:
        return (
            "post_write_verification"
            if phase in {"post-write verification", "post-write-verification"}
            else phase.replace("-", "_")
        )
    text = str(exc).lower()
    if "post-write" in text or "cache refresh failed" in text or "verification" in text:
        return "post_write_verification"
    if "worksheet" in text and "lookup" in text:
        return "worksheet_lookup"
    if "update" in text or "write" in text:
        return "worksheet_update"
    return "preflight"


def _log_quota_summary(summary: availability.AvailabilityQuotaSummary | None) -> None:
    if summary is None:
        return
    log.info(
        "setopenspots quota summary",
        extra={
            "command": summary.command,
            "clan_tag": summary.clan_tag,
            "operation": summary.operation,
            "sheet_reads_count": summary.sheet_reads_count,
            "sheet_writes_count": summary.sheet_writes_count,
            "cache_hit": summary.cache_hit,
            "cache_miss": summary.cache_miss,
            "refreshed_clans_cache": summary.refreshed_clans_cache,
            "used_cached_header": summary.used_cached_header,
            "used_cached_row": summary.used_cached_row,
        },
    )


def _failure_message_for(exc: BaseException) -> str:
    if availability.is_rate_limited_error(exc):
        return QUOTA_FAILURE_MESSAGE
    phase = _operation_phase(exc)
    if phase in {"preflight", "worksheet_lookup"}:
        return ROW_CONFIG_FAILURE_MESSAGE
    if phase == "post_write_verification":
        return VERIFY_FAILURE_MESSAGE
    return WRITE_FAILURE_MESSAGE


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

        caller_source = _caller_source(ctx)
        quota_token = availability.begin_quota_summary(clan_tag_or_name)
        try:
            try:
                old_value, corrected_value, resolved_clan = (
                    await availability.set_manual_open_spots(
                        clan_tag_or_name, new_value
                    )
                )
            except ValueError as exc:
                if _is_ambiguous_clan_error(exc):
                    await ctx.reply(
                        "That clan input matches multiple clans; please use the clan tag.",
                        mention_author=False,
                    )
                    return
                if _is_clan_not_found_error(exc):
                    await ctx.reply(CLAN_NOT_FOUND_MESSAGE, mention_author=False)
                    return
                phase = _operation_phase(exc)
                if _is_config_or_preflight_error(exc):
                    log.error(
                        "setopenspots failed",
                        exc_info=True,
                        extra={
                            "clan_tag": clan_tag_or_name,
                            "requested_open_spots": new_value,
                            "caller_source": caller_source,
                            "operation_phase": phase,
                            "exception_type": type(exc).__name__,
                            "exception_message": str(exc),
                            "quota_exhausted": availability.is_rate_limited_error(exc),
                        },
                    )
                    await runtime_helpers.send_log_message(
                        f"⚠️ Open spots correction failed during {phase}: recruitment sheet configuration invalid for {clan_tag_or_name}."
                    )
                    await ctx.reply(CONFIG_FAILURE_MESSAGE, mention_author=False)
                    return
                log.error(
                    "setopenspots failed",
                    exc_info=True,
                    extra={
                        "clan_tag": clan_tag_or_name,
                        "requested_open_spots": new_value,
                        "caller_source": caller_source,
                        "operation_phase": phase,
                        "exception_type": type(exc).__name__,
                        "exception_message": str(exc),
                        "quota_exhausted": availability.is_rate_limited_error(exc),
                    },
                )
                await runtime_helpers.send_log_message(
                    f"⚠️ Open spots correction failed during {phase} for {clan_tag_or_name}: {type(exc).__name__}: {exc}"
                )
                await ctx.reply(_failure_message_for(exc), mention_author=False)
                return
            except Exception as exc:
                phase = _operation_phase(exc)
                quota = availability.is_rate_limited_error(exc)
                log.error(
                    "setopenspots failed",
                    exc_info=True,
                    extra={
                        "clan_tag": clan_tag_or_name,
                        "requested_open_spots": new_value,
                        "caller_source": caller_source,
                        "operation_phase": phase,
                        "exception_type": type(exc).__name__,
                        "exception_message": str(exc),
                        "quota_exhausted": quota,
                    },
                )
                failure_reason = (
                    "quota_exhausted" if quota else f"{type(exc).__name__}: {exc}"
                )
                await runtime_helpers.send_log_message(
                    f"⚠️ Open spots correction failed during {phase} for {clan_tag_or_name}: {failure_reason}"
                )
                await ctx.reply(_failure_message_for(exc), mention_author=False)
                return
        finally:
            quota_summary = availability.end_quota_summary(quota_token)
            _log_quota_summary(quota_summary)

        actor = _display_name(ctx.author)
        actor_id = getattr(ctx.author, "id", None)
        reason_text = reason.strip()
        success_message = (
            "**Open spots corrected**\n\n"
            f"Clan: {resolved_clan}\n"
            f"Open spots: {old_value} → {corrected_value}\n"
            f"Reason: {reason_text}\n"
            f"Updated by: {actor}"
        )
        id_suffix = f" ({actor_id})" if actor_id is not None else ""
        audit_message = (
            f"🛠️ Open spots corrected: {resolved_clan} {old_value} → {corrected_value} by {actor}{id_suffix}\n"
            f"Reason: {reason_text}"
        )
        try:
            await ctx.reply(success_message, mention_author=False)
        except Exception:
            log.error(
                "setopenspots success reply failed after sheet update", exc_info=True
            )
            await runtime_helpers.send_log_message(
                f"⚠️ Open spots correction succeeded for {resolved_clan}, but the Discord success reply failed."
            )
            return
        await runtime_helpers.send_log_message(audit_message)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RecruitmentOpenSpotsCog(bot))
