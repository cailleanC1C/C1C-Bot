"""Fusion opt-in role cleanup for ended fusions."""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

import discord
from discord.ext import commands

from modules.community.fusion.announcements import resolve_announcement_channel
from modules.community.fusion import logs as fusion_logs
from shared.sheets import fusion as fusion_sheets

log = logging.getLogger("c1c.community.fusion.role_cleanup")

_ROLE_CLEANUP_EVENT_ID = "__fusion_role_cleanup__"
_ROLE_CLEANUP_TYPE = "ended"
_ROLE_CLEANUP_DEDUPE_KEY = (_ROLE_CLEANUP_EVENT_ID, _ROLE_CLEANUP_TYPE)


@dataclass(slots=True)
class FusionRoleCleanupSummary:
    fusion_id: str
    fusion_name: str
    role_id: int | None
    role_name: str | None = None
    members_found: int = 0
    removed_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    dedupe_key: str = f"{_ROLE_CLEANUP_EVENT_ID}:{_ROLE_CLEANUP_TYPE}"
    status: str = "processed"
    already_processed: bool = False
    failure_reasons: list[str] = field(default_factory=list)
    row_number: int | None = None


_RECENT_ROLE_CLEANUP_SUMMARIES: list[FusionRoleCleanupSummary] = []


def get_recent_role_cleanup_summaries() -> list[FusionRoleCleanupSummary]:
    return list(_RECENT_ROLE_CLEANUP_SUMMARIES)


def clear_recent_role_cleanup_summaries() -> None:
    _RECENT_ROLE_CLEANUP_SUMMARIES.clear()


def _record_summary(summary: FusionRoleCleanupSummary) -> None:
    for idx, existing in enumerate(_RECENT_ROLE_CLEANUP_SUMMARIES):
        if existing.fusion_id == summary.fusion_id and existing.dedupe_key == summary.dedupe_key:
            if summary.already_processed and not existing.already_processed:
                return
            _RECENT_ROLE_CLEANUP_SUMMARIES[idx] = summary
            return
    _RECENT_ROLE_CLEANUP_SUMMARIES.append(summary)


def _summary_to_payload(summary: FusionRoleCleanupSummary) -> dict[str, object]:
    return {
        "fusion_id": summary.fusion_id,
        "fusion_name": summary.fusion_name,
        "role_id": summary.role_id,
        "role_name": summary.role_name,
        "members_found": summary.members_found,
        "removed_count": summary.removed_count,
        "failed_count": summary.failed_count,
        "skipped_count": summary.skipped_count,
        "dedupe_key": summary.dedupe_key,
        "status": summary.status,
        "already_processed": summary.already_processed,
        "failure_reasons": list(summary.failure_reasons),
    }


def _summary_from_payload(payload: dict[str, object]) -> FusionRoleCleanupSummary:
    def _to_int(value: object) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    role_id_value = payload.get("role_id")
    role_id = None if role_id_value in (None, "") else _to_int(role_id_value)
    failure_reasons = payload.get("failure_reasons")
    if not isinstance(failure_reasons, list):
        failure_reasons = []
    return FusionRoleCleanupSummary(
        fusion_id=str(payload.get("fusion_id") or ""),
        fusion_name=str(payload.get("fusion_name") or ""),
        role_id=role_id,
        role_name=str(payload.get("role_name") or "") or None,
        members_found=_to_int(payload.get("members_found")),
        removed_count=_to_int(payload.get("removed_count")),
        failed_count=_to_int(payload.get("failed_count")),
        skipped_count=_to_int(payload.get("skipped_count")),
        dedupe_key=str(payload.get("dedupe_key") or f"{_ROLE_CLEANUP_EVENT_ID}:{_ROLE_CLEANUP_TYPE}"),
        status=str(payload.get("status") or "processed"),
        already_processed=bool(payload.get("already_processed")),
        failure_reasons=[str(reason) for reason in failure_reasons],
        row_number=_to_int(payload.get("_row_number")) or None,
    )


async def _persist_summary(summary: FusionRoleCleanupSummary, *, sent_at: dt.datetime) -> None:
    try:
        await fusion_sheets.upsert_role_cleanup_summary(
            summary.fusion_id,
            payload=_summary_to_payload(summary),
            sent_at=sent_at,
        )
    except Exception as exc:
        context = {"fusion_id": summary.fusion_id, "role_id": summary.role_id}
        log.exception("fusion role cleanup summary persist failed", extra=context)
        try:
            await fusion_logs.send_ops_alert(
                component="role_cleanup",
                summary="summary_persist_failed",
                dedupe_key=f"fusion:role_cleanup:summary:{summary.fusion_id}",
                error=exc,
                fields=context,
            )
        except Exception:
            log.exception("fusion role cleanup summary persist alert failed", extra=context)


async def load_unreported_role_cleanup_summaries() -> list[FusionRoleCleanupSummary]:
    payloads = await fusion_sheets.get_unreported_role_cleanup_summaries()
    if payloads:
        return [_summary_from_payload(dict(payload)) for payload in payloads]
    return get_recent_role_cleanup_summaries()


async def mark_role_cleanup_summaries_reported(
    summaries: list[FusionRoleCleanupSummary],
) -> None:
    row_numbers = [summary.row_number for summary in summaries if summary.row_number]
    if row_numbers:
        await fusion_sheets.mark_role_cleanup_summaries_reported(row_numbers)
    clear_recent_role_cleanup_summaries()


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

        if _ROLE_CLEANUP_DEDUPE_KEY in sent_keys:
            try:
                if await fusion_sheets.has_role_cleanup_summary(target.fusion_id):
                    continue
            except Exception:
                log.warning(
                    "fusion role cleanup summary state lookup failed",
                    exc_info=True,
                    extra={"fusion_id": target.fusion_id},
                )
            summary = FusionRoleCleanupSummary(
                fusion_id=target.fusion_id,
                fusion_name=target.fusion_name,
                role_id=target.opt_in_role_id,
                status="skipped",
                already_processed=True,
                skipped_count=1,
            )
            _record_summary(summary)
            await _persist_summary(summary, sent_at=reference)
            continue

        summary = FusionRoleCleanupSummary(
            fusion_id=target.fusion_id,
            fusion_name=target.fusion_name,
            role_id=target.opt_in_role_id,
        )
        try:
            guild = await _resolve_cleanup_guild(bot, target)
            if guild is None:
                summary.status = "skipped"
                summary.skipped_count += 1
                summary.failure_reasons.append("guild unavailable")
                log.warning(
                    "fusion role cleanup skipped; guild unavailable",
                    extra={"fusion_id": target.fusion_id, "role_id": target.opt_in_role_id},
                )
                _record_summary(summary)
                await _persist_summary(summary, sent_at=reference)
                continue

            role = guild.get_role(target.opt_in_role_id)
            if role is None:
                summary.status = "skipped"
                summary.skipped_count += 1
                summary.failure_reasons.append("role missing")
                log.warning(
                    "fusion role cleanup role missing",
                    extra={"fusion_id": target.fusion_id, "guild_id": guild.id, "role_id": target.opt_in_role_id},
                )
            else:
                summary.role_name = getattr(role, "name", None)
                summary.members_found = len(list(getattr(role, "members", [])))
                for member in list(role.members):
                    try:
                        await member.remove_roles(role, reason=f"Fusion ended: {target.fusion_id}")
                        summary.removed_count += 1
                    except Exception:
                        summary.failed_count += 1
                        summary.failure_reasons.append(
                            f"member {getattr(member, 'id', 'unknown')}: permission/hierarchy/API failure"
                        )
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
            if summary.failed_count:
                summary.status = "partial_failure"
            _record_summary(summary)
            await _persist_summary(summary, sent_at=reference)
        except Exception as exc:
            context = {"fusion_id": target.fusion_id, "role_id": target.opt_in_role_id}
            summary.status = "failed"
            summary.failed_count += 1
            summary.failure_reasons.append(str(exc))
            _record_summary(summary)
            await _persist_summary(summary, sent_at=reference)
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


__all__ = [
    "FusionRoleCleanupSummary",
    "clear_recent_role_cleanup_summaries",
    "get_recent_role_cleanup_summaries",
    "load_unreported_role_cleanup_summaries",
    "mark_role_cleanup_summaries_reported",
    "process_ended_fusion_role_cleanup",
]
