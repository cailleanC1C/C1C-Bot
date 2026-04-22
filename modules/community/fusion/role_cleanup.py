"""Fusion opt-in role cleanup for ended fusions."""

from __future__ import annotations

import datetime as dt
import logging

import discord
from discord.ext import commands

from modules.community.fusion.announcements import resolve_announcement_channel
from modules.community.fusion import logs as fusion_logs
from shared.sheets import fusion as fusion_sheets

log = logging.getLogger("c1c.community.fusion.role_cleanup")

_ROLE_CLEANUP_EVENT_ID = "__fusion_role_cleanup__"
_ROLE_CLEANUP_TYPE = "ended"


def _utc_now(now: dt.datetime | None = None) -> dt.datetime:
    if now is None:
        return dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=dt.timezone.utc)
    return now.astimezone(dt.timezone.utc)


async def _resolve_cleanup_guild(bot: commands.Bot, target: fusion_sheets.FusionRow) -> discord.Guild | None:
    channel = await resolve_announcement_channel(bot, target.announcement_channel_id)
    if isinstance(channel, discord.abc.GuildChannel):
        return channel.guild

    if target.opt_in_role_id is None:
        return None

    for guild in bot.guilds:
        if guild.get_role(target.opt_in_role_id) is not None:
            return guild
    return None


async def process_ended_fusion_role_cleanup(
    bot: commands.Bot,
    *,
    now: dt.datetime | None = None,
) -> None:
    reference = _utc_now(now)

    try:
        ended_fusions = await fusion_sheets.get_ended_fusions(now=reference)
    except Exception as exc:
        log.exception("fusion role cleanup failed to load ended fusions")
        await fusion_logs.send_ops_alert(
            component="role_cleanup",
            summary="load_ended_fusions_failed",
            dedupe_key="fusion:role_cleanup:load_ended",
            error=exc,
        )
        return

    for target in ended_fusions:
        try:
            await fusion_sheets.transition_fusion_to_ended(target.fusion_id)
        except Exception as exc:
            context = {"fusion_id": target.fusion_id}
            log.exception("fusion status transition to ended failed", extra=context)
            await fusion_logs.send_ops_alert(
                component="role_cleanup",
                summary="status_transition_failed",
                dedupe_key=f"fusion:role_cleanup:status:{target.fusion_id}",
                error=exc,
                fields=context,
            )

        if target.opt_in_role_id is None:
            continue

        try:
            sent_keys = await fusion_sheets.get_sent_reminder_keys(target.fusion_id)
        except Exception as exc:
            context = {"fusion_id": target.fusion_id}
            log.exception(
                "fusion role cleanup failed to load dedupe state",
                extra=context,
            )
            await fusion_logs.send_ops_alert(
                component="role_cleanup",
                summary="load_dedupe_state_failed",
                dedupe_key=f"fusion:role_cleanup:dedupe:{target.fusion_id}",
                error=exc,
                fields=context,
            )
            continue

        cleanup_key = (_ROLE_CLEANUP_EVENT_ID, _ROLE_CLEANUP_TYPE)
        if cleanup_key in sent_keys:
            continue

        try:
            guild = await _resolve_cleanup_guild(bot, target)
            if guild is None:
                log.warning(
                    "fusion role cleanup skipped; guild unavailable",
                    extra={"fusion_id": target.fusion_id, "role_id": target.opt_in_role_id},
                )
                continue

            role = guild.get_role(target.opt_in_role_id)
            if role is None:
                log.warning(
                    "fusion role cleanup role missing",
                    extra={"fusion_id": target.fusion_id, "guild_id": guild.id, "role_id": target.opt_in_role_id},
                )
            else:
                for member in list(role.members):
                    try:
                        await member.remove_roles(role, reason=f"Fusion ended: {target.fusion_id}")
                    except Exception:
                        log.exception(
                            "fusion role cleanup failed for member",
                            extra={
                                "fusion_id": target.fusion_id,
                                "guild_id": guild.id,
                                "role_id": role.id,
                                "user_id": member.id,
                            },
                        )

            await fusion_sheets.mark_reminder_sent(
                target.fusion_id,
                event_id=_ROLE_CLEANUP_EVENT_ID,
                reminder_type=_ROLE_CLEANUP_TYPE,
                sent_at=reference,
            )
        except Exception as exc:
            context = {"fusion_id": target.fusion_id, "role_id": target.opt_in_role_id}
            log.exception(
                "fusion role cleanup iteration failed",
                extra=context,
            )
            await fusion_logs.send_ops_alert(
                component="role_cleanup",
                summary="iteration_failed",
                dedupe_key=f"fusion:role_cleanup:iteration:{target.fusion_id}",
                error=exc,
                fields=context,
            )


__all__ = ["process_ended_fusion_role_cleanup"]
