"""Fusion role and personal-progress button view helpers."""

from __future__ import annotations

import datetime as dt
import logging
import time
from collections.abc import Mapping, Sequence
from typing import Literal

import discord
from discord.ext import commands

from modules.community.fusion import announcements as fusion_announcements
from modules.community.fusion import logs as fusion_logs
from modules.community.fusion.progress_share import build_progress_share_embed, build_share_snapshot
from shared.sheets import fusion as fusion_sheets

log = logging.getLogger("c1c.community.fusion.opt_in")

_FUSION_OPT_IN_CUSTOM_ID = "fusion:opt_in"
_FUSION_OPT_OUT_CUSTOM_ID = "fusion:opt_out"
_FUSION_MY_PROGRESS_CUSTOM_ID = "fusion:my_progress"
_FUSION_PROGRESS_EVENT_CUSTOM_ID = "fusion:progress:event"
_FUSION_PROGRESS_STATUS_CUSTOM_ID = "fusion:progress:status"
_FUSION_PROGRESS_SHARE_SUMMARY_CUSTOM_ID = "fusion:progress:share:summary"
_FUSION_PROGRESS_SHARE_DETAILED_CUSTOM_ID = "fusion:progress:share:detailed"
_FUSION_PROGRESS_MARK_ALL_CUSTOM_ID = "fusion:progress:mark_all"
_FUSION_PROGRESS_UPDATE_CUSTOM_ID = "fusion:progress:update"
_FUSION_TRAD_CHOICE_EVENT_CUSTOM_ID = "fusion:traditional:choice:event"
_FUSION_TRAD_CHOICE_PREP_CUSTOM_ID = "fusion:traditional:choice:prep"
_FUSION_TRAD_PREP_UPDATE_CUSTOM_ID = "fusion:traditional:prep:update"
_FUSION_TRAD_BACK_CUSTOM_ID = "fusion:traditional:back"

_DISPLAY_STATUS_ORDER = ("done", "in_progress", "skipped", "missed", "not_started")
_EVENT_DROPDOWN_STATUS_ORDER = ("not_started", "in_progress", "missed", "skipped", "done", "done_bonus")
_STATUS_LABELS = {
    "not_started": "Not Started",
    "in_progress": "In Progress",
    "done": "Done",
    "done_bonus": "Done + Bonus",
    "skipped": "Skipped",
    "missed": "Missed",
}
_STATUS_ICONS = {
    "done": "✅",
    "done_bonus": "✅",
    "in_progress": "🟡",
    "skipped": "⏭️",
    "missed": "⚠️",
    "not_started": "⬜",
}
_SHARE_MODE_LABELS = {"summary": "Summary", "detailed": "Detailed"}
_ALLOWED_PROGRESS_STATES = frozenset({"not_started", "in_progress", "done", "done_bonus", "skipped"})
_STATUS_INDEX_TO_CANONICAL = {
    "0": "not_started",
    "1": "in_progress",
    "2": "done",
    "3": "done_bonus",
    "4": "skipped",
}
_EVENT_DROPDOWN_STATUS_RANK = {status: idx for idx, status in enumerate(_EVENT_DROPDOWN_STATUS_ORDER)}
_PROGRESS_EVENT_PAGE_SIZE = 10



def _is_traditional_fusion(target: fusion_sheets.FusionRow) -> bool:
    return str(target.fusion_type or "").strip().casefold() == "traditional"


def _traditional_rares_acquired(
    *,
    target: fusion_sheets.FusionRow,
    events: Sequence[fusion_sheets.FusionEventRow],
    progress_by_event: Mapping[str, str],
) -> int:
    total = 0.0
    for event in events:
        if str(event.reward_type or "").strip().casefold() not in {"rare", "rares"}:
            continue
        if progress_by_event.get(event.event_id) in {"done", "done_bonus"}:
            total += max(0.0, float(event.reward_amount or 0))
    return min(max(0, int(total)), max(0, int(target.needed)))


def _validate_traditional_prep_counts(
    *,
    needed_total: int,
    rares_acquired: int,
    rares_level_40: int,
    rares_ascended: int,
    epics_fused: int,
    epics_level_50: int,
    epics_ascended: int,
) -> str | None:
    values = {
        "Rares level 40": rares_level_40,
        "Rares fully ascended": rares_ascended,
        "Epics fused": epics_fused,
        "Epics level 50": epics_level_50,
        "Epics fully ascended": epics_ascended,
    }
    if any(value < 0 for value in values.values()):
        return "All champion preparation counts must be non-negative integers."
    if rares_level_40 > needed_total or rares_ascended > needed_total:
        return f"Rare prep counts cannot be higher than {needed_total}."
    if epics_fused > 4 or epics_level_50 > 4 or epics_ascended > 4:
        return "Epic prep counts cannot be higher than 4."
    if rares_level_40 > rares_acquired:
        return "Rares level 40 cannot be higher than Rares acquired from event rewards."
    if rares_ascended > rares_level_40:
        return "Rares fully ascended cannot be higher than Rares level 40."
    if epics_fused > rares_ascended // 4:
        return "Epics fused cannot be higher than the number allowed by fully ascended Rares."
    if epics_level_50 > epics_fused:
        return "Epics level 50 cannot be higher than Epics fused."
    if epics_ascended > epics_level_50:
        return "Epics fully ascended cannot be higher than Epics level 50."
    return None


def _build_traditional_prep_embed(
    *,
    target: fusion_sheets.FusionRow,
    events: Sequence[fusion_sheets.FusionEventRow],
    progress_by_event: Mapping[str, str],
    prep: fusion_sheets.FusionTraditionalUserProgressRow,
) -> discord.Embed:
    needed_total = max(0, int(target.needed))
    rares_still_needed = max(0, needed_total - prep.rares_owned)
    ready = prep.target_ready
    missing = "Nothing, the target champion is ready to fuse." if ready else f"{max(0, 4 - prep.epics_ascended)} fully ascended Epics"
    embed = discord.Embed(
        title=f"Champion Preparation: {target.champion or target.fusion_name}",
        description="Traditional fusion prep tracker.",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Rare Progress", value=f"Rares owned: {prep.rares_owned} / {needed_total}\nRares still needed: {rares_still_needed}", inline=False)
    embed.add_field(name="Rare Prep", value=f"Rares level 40: {prep.rares_level_40} / {needed_total}\nRares fully ascended: {prep.rares_ascended} / {needed_total}", inline=False)
    embed.add_field(name="Epic Prep", value=f"Epics fused: {prep.epics_fused} / 4\nEpics level 50: {prep.epics_level_50} / 4\nEpics fully ascended: {prep.epics_ascended} / 4", inline=False)
    embed.add_field(name="Final Fusion", value=f"Ready to fuse {target.champion or target.fusion_name}: {'Yes' if ready else 'No'}\nMissing: {missing}", inline=False)
    return embed

def _supports_partial_fragments(event: fusion_sheets.FusionEventRow | None, *, status: str | None) -> bool:
    if event is None:
        return False
    return (
        status in {"in_progress", "done", "done_bonus"}
        and event.reward_amount > 0
        and str(event.reward_type or "").strip().lower() == "fragment"
        and bool(event.milestones)
    )


def _effective_display_status(
    *,
    event: fusion_sheets.FusionEventRow,
    progress_by_event: Mapping[str, str],
    now: dt.datetime | None = None,
) -> str:
    status = progress_by_event.get(event.event_id, "not_started")
    if status not in _ALLOWED_PROGRESS_STATES:
        status = "not_started"
    if status in {"done", "done_bonus"}:
        return status

    if now is None:
        now = dt.datetime.now(dt.timezone.utc)
    timing = fusion_sheets.get_valid_event_timing(event, for_helper="fusion_my_progress")
    if timing is None:
        return status
    start_at, end_at = timing
    if status == "not_started" and fusion_sheets.derive_event_status(start_at_utc=start_at, end_at_utc=end_at, now=now) == "ended":
        return "missed"
    return status


def _coerce_status_for_save(raw_status: object) -> str | None:
    token = str(raw_status or "").strip().lower()
    if token in _ALLOWED_PROGRESS_STATES:
        return token
    return _STATUS_INDEX_TO_CANONICAL.get(token)


def _normalize_progress_payload(payload: object) -> tuple[dict[str, str], bool]:
    if isinstance(payload, Mapping) and "progress" in payload and isinstance(payload.get("progress"), Mapping):
        candidate = payload.get("progress")
    else:
        candidate = payload

    if not isinstance(candidate, Mapping):
        return {}, True

    normalized: dict[str, str] = {}
    for key, value in candidate.items():
        event_id = str(key or "").strip()
        if not event_id:
            continue
        status = str(value or "").strip().lower()
        if status not in {"not_started", "in_progress", "done", "done_bonus", "skipped", "missed"}:
            status = "not_started"
        normalized[event_id] = status
    return normalized, False


def _normalize_partial_payload(payload: object) -> dict[str, float]:
    candidate = payload.get("partials") if isinstance(payload, Mapping) else {}
    partials: dict[str, float] = {}
    if isinstance(candidate, Mapping):
        for key, value in candidate.items():
            event_id = str(key or "").strip()
            if not event_id:
                continue
            try:
                partials[event_id] = max(0.0, float(str(value).strip() or "0"))
            except ValueError:
                continue
    return partials


def _event_bonus_amount(event: fusion_sheets.FusionEventRow) -> float:
    return event.bonus if event.bonus is not None else 0.0


def _event_has_bonus(event: fusion_sheets.FusionEventRow) -> bool:
    return _event_bonus_amount(event) > 0


def _event_reward_label(event: fusion_sheets.FusionEventRow) -> str:
    reward_unit = str(event.reward_type or "").strip() or "rewards"
    bonus = _event_bonus_amount(event)
    if bonus > 0:
        return f"{event.reward_amount:g} + {bonus:g} bonus {reward_unit}"
    return f"{event.reward_amount:g} {reward_unit}"


async def _send_or_followup_ephemeral(
    interaction: discord.Interaction,
    *,
    content: str | None = None,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(content=content, embed=embed, view=view, ephemeral=True)
        return
    await interaction.response.send_message(content=content, embed=embed, view=view, ephemeral=True)


async def _send_ephemeral(interaction: discord.Interaction, message: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
        return
    await interaction.response.send_message(message, ephemeral=True)


async def _resolve_member(interaction: discord.Interaction) -> discord.Member | None:
    if isinstance(interaction.user, discord.Member):
        return interaction.user

    guild = interaction.guild
    if guild is None:
        return None

    member = guild.get_member(interaction.user.id)
    if member is not None:
        return member

    try:
        return await guild.fetch_member(interaction.user.id)
    except Exception:
        return None


async def _resolve_opt_in_role(interaction: discord.Interaction) -> tuple[discord.Member | None, discord.Role | None]:
    target = await fusion_sheets.get_publishable_fusion()
    role_id = target.opt_in_role_id if target is not None else None
    if role_id is None:
        await _send_ephemeral(interaction, "No fusion opt-in role is configured.")
        return None, None

    guild = interaction.guild
    if guild is None:
        await _send_ephemeral(interaction, "Fusion role actions only work in a server.")
        return None, None

    member = await _resolve_member(interaction)
    if member is None:
        await _send_ephemeral(interaction, "Couldn’t resolve your member record right now.")
        return None, None

    role = guild.get_role(role_id)
    if role is None:
        log.warning(
            "fusion opt-in role missing in guild",
            extra={"guild_id": guild.id, "role_id": role_id},
        )
        await _send_ephemeral(interaction, "Fusion role is missing in this server.")
        return member, None

    return member, role


async def _handle_opt_action(interaction: discord.Interaction, *, action: Literal["in", "out"]) -> None:
    try:
        member, role = await _resolve_opt_in_role(interaction)
    except Exception as exc:
        context = fusion_logs.interaction_context(interaction, custom_id=f"fusion:opt_{action}")
        log.exception("fusion opt button failed to resolve role", extra=context)
        await fusion_logs.send_ops_alert(
            component="opt_button",
            summary="resolve_opt_in_role_failed",
            dedupe_key=f"fusion:opt_role:{action}",
            error=exc,
            fields=context,
        )
        await _send_ephemeral(interaction, "Temporary issue. Try again shortly.")
        return

    if member is None or role is None:
        return

    has_role = role in member.roles

    if action == "in":
        if has_role:
            await _send_ephemeral(interaction, "Already opted in.")
            return
        try:
            await member.add_roles(role, reason="Fusion role opt-in button")
        except Exception as exc:
            context = {"guild_id": member.guild.id, "user_id": member.id, "role_id": role.id, "custom_id": _FUSION_OPT_IN_CUSTOM_ID}
            log.exception("fusion opt-in add role failed", extra=context)
            await fusion_logs.send_ops_alert(
                component="opt_button",
                summary="add_role_failed",
                dedupe_key=f"fusion:opt_in:add_role:{member.guild.id}:{role.id}",
                error=exc,
                fields=context,
            )
            await _send_ephemeral(interaction, "Couldn’t update your fusion role right now.")
            return
        await _send_ephemeral(interaction, "Opted in. You’ll get fusion pings.")
        return

    if not has_role:
        await _send_ephemeral(interaction, "You’re already opted out.")
        return

    try:
        await member.remove_roles(role, reason="Fusion role opt-out button")
    except Exception as exc:
        context = {"guild_id": member.guild.id, "user_id": member.id, "role_id": role.id, "custom_id": _FUSION_OPT_OUT_CUSTOM_ID}
        log.exception("fusion opt-out remove role failed", extra=context)
        await fusion_logs.send_ops_alert(
            component="opt_button",
            summary="remove_role_failed",
            dedupe_key=f"fusion:opt_out:remove_role:{member.guild.id}:{role.id}",
            error=exc,
            fields=context,
        )
        await _send_ephemeral(interaction, "Couldn’t update your fusion role right now.")
        return

    await _send_ephemeral(interaction, "Opted out. No more fusion pings.")


def _build_progress_summary_embed(
    *,
    target: fusion_sheets.FusionRow,
    events: Sequence[fusion_sheets.FusionEventRow],
    progress_by_event: dict[str, str],
    partial_by_event: Mapping[str, float] | None = None,
    selected_event_id: str | None = None,
    last_update: tuple[str, str] | None = None,
) -> discord.Embed:
    snapshot = build_share_snapshot(events=events, progress_by_event=progress_by_event, partial_by_event=partial_by_event)
    raw_reward_type = str(target.reward_type or "").strip()
    lowered_reward_type = raw_reward_type.lower()
    if lowered_reward_type in {"fragment", "fragments"}:
        reward_label = "Fragment"
    elif raw_reward_type:
        reward_label = raw_reward_type.title()
    else:
        reward_label = "Reward"
    acquired = snapshot.completed_reward_total
    skipped = snapshot.skipped_reward_total
    still_needed = max(float(target.needed) - acquired, 0.0)
    still_needed_line = "Fusion ready" if still_needed <= 0 else f"{still_needed:g} to go"

    embed = discord.Embed(
        title=f"My Progress: {target.fusion_name}",
        description="Private tracker for your fusion progress.",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="Summary",
        value=(
            f"✅ Done: {snapshot.counts['done']}\n"
            f"🟡 In Progress: {snapshot.counts['in_progress']}\n"
            f"⏭️ Skipped: {snapshot.counts['skipped']}\n"
            f"⚠️ Missed: {snapshot.counts['missed']}\n"
            f"⬜ Not Started: {snapshot.counts['not_started']}"
        ),
        inline=False,
    )
    embed.add_field(
        name="\u200b",
        value=(
            f"**{reward_label} Progress**\n"
            f"{acquired:g} acquired\n"
            f"{skipped:g} skipped\n"
            f"{still_needed_line}\n\n"
            f"{target.needed:g} / {target.available:g} needed"
        ),
        inline=False,
    )

    if selected_event_id:
        selected = next((event for event in events if event.event_id == selected_event_id), None)
        if selected is not None:
            current = snapshot.display_status_by_event.get(selected.event_id, "not_started")
            icon = _STATUS_ICONS.get(current, _STATUS_ICONS["not_started"])
            partial_amount = max(0.0, float((partial_by_event or {}).get(selected.event_id, 0.0)))
            embed.add_field(
                name="Selected Event",
                value=(
                    f"{icon} {selected.event_name}\n{_event_reward_label(selected)}"
                    + (
                        f"\nPartial logged: {partial_amount:g} / {selected.reward_amount:g} fragments"
                        if _supports_partial_fragments(selected, status=current) and partial_amount > 0
                        else ""
                    )
                ),
                inline=False,
            )

    last_update_value = "No changes yet."
    if last_update is not None:
        event_name, status = last_update
        last_update_value = f"{event_name} → {_STATUS_LABELS.get(status, 'Not Started')}"
    embed.add_field(name="Last Update", value=last_update_value, inline=False)

    return embed


def _ordered_progress_events(
    events: Sequence[fusion_sheets.FusionEventRow],
    progress_by_event: Mapping[str, str],
) -> list[fusion_sheets.FusionEventRow]:
    now = dt.datetime.now(dt.timezone.utc)
    event_rows = list(enumerate(events))
    ordered_events = sorted(
        event_rows,
        key=lambda item: (
            _EVENT_DROPDOWN_STATUS_RANK.get(
                _effective_display_status(event=item[1], progress_by_event=progress_by_event, now=now),
                len(_EVENT_DROPDOWN_STATUS_RANK),
            ),
            item[0],
        ),
    )
    return [event for _, event in ordered_events]


class _FusionProgressEventSelect(discord.ui.Select):
    def __init__(
        self,
        events: Sequence[fusion_sheets.FusionEventRow],
        selected_event_id: str | None,
        progress_by_event: Mapping[str, str],
        *,
        page_index: int = 0,
        page_count: int = 1,
    ) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        options: list[discord.SelectOption] = []
        for event in events[:_PROGRESS_EVENT_PAGE_SIZE]:
            status = _effective_display_status(event=event, progress_by_event=progress_by_event, now=now)
            icon = _STATUS_ICONS.get(status, _STATUS_ICONS["not_started"])
            options.append(
                discord.SelectOption(
                    label=f"{icon} {event.event_name or event.event_id}"[:100],
                    value=event.event_id,
                    default=event.event_id == selected_event_id,
                )
            )
        placeholder = "Choose event"
        if page_count > 1:
            placeholder = f"Choose event (page {page_index + 1}/{page_count})"
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
            custom_id=_FUSION_PROGRESS_EVENT_CUSTOM_ID,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, FusionProgressPanelView):
            return
        if interaction.user.id != view.user_id:
            await _send_ephemeral(interaction, "This progress panel belongs to a different user.")
            return

        selected_event_id = self.values[0] if self.values else ""
        view.selected_event_id = selected_event_id or None
        view.refresh_items()
        await _edit_progress_message(interaction, view=view)


class _FusionProgressStatusSelect(discord.ui.Select):
    def __init__(self, selected_status: str | None, *, selected_event: fusion_sheets.FusionEventRow | None) -> None:
        options = [
            discord.SelectOption(label="Not Started", value="not_started", default=selected_status == "not_started"),
            discord.SelectOption(label="In Progress", value="in_progress", default=selected_status == "in_progress"),
            discord.SelectOption(label="Done", value="done", default=selected_status == "done"),
        ]
        if selected_event is not None and _event_has_bonus(selected_event):
            options.append(discord.SelectOption(label="Done + Bonus", value="done_bonus", default=selected_status == "done_bonus"))
        options.append(discord.SelectOption(label="Skipped", value="skipped", default=selected_status == "skipped"))
        super().__init__(
            placeholder="Set status",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=_FUSION_PROGRESS_STATUS_CUSTOM_ID,
            row=1,
            disabled=selected_status is None,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, FusionProgressPanelView):
            return
        if interaction.user.id != view.user_id:
            await _send_ephemeral(interaction, "This progress panel belongs to a different user.")
            return
        selected_event_id = view.selected_event_id
        if not selected_event_id:
            await _send_ephemeral(interaction, "Choose an event first.")
            return

        selected_status = self.values[0] if self.values else "not_started"
        status = _coerce_status_for_save(selected_status)
        if status is None:
            context = {
                "fusion_id": view.fusion_id,
                "event_id": selected_event_id,
                "user_id": view.user_id,
                "status": str(selected_status),
                "custom_id": _FUSION_PROGRESS_STATUS_CUSTOM_ID,
            }
            log.error("fusion progress status invalid; aborting save", extra=context)
            await _send_ephemeral(interaction, "Couldn’t save progress right now. Please choose a valid status.")
            return
        event = view.events_by_id.get(selected_event_id)
        if event is None:
            await _send_ephemeral(interaction, "That event is no longer available. Reopen My Progress.")
            return

        now = dt.datetime.now(dt.timezone.utc)
        log.info(
            "fusion progress status save requested",
            extra={
                "fusion_id": view.fusion_id,
                "event_id": event.event_id,
                "user_id": view.user_id,
                "status": status,
            },
        )
        await _safe_defer_progress_interaction(interaction)
        try:
            partial_amount = view.partial_by_event.get(event.event_id)
            if status in {"not_started", "skipped"}:
                partial_amount = None
            await fusion_sheets.upsert_user_event_progress(
                view.fusion_id,
                str(view.user_id),
                event.event_id,
                status,
                now,
                partial_amount=partial_amount,
            )
        except Exception as exc:
            context = {
                "fusion_id": view.fusion_id,
                "event_id": event.event_id,
                "user_id": view.user_id,
                "custom_id": _FUSION_PROGRESS_STATUS_CUSTOM_ID,
            }
            await _send_progress_save_failure(
                interaction,
                component="my_progress",
                dedupe_key=f"fusion:progress:update:{view.fusion_id}:{event.event_id}",
                error=exc,
                fields=context,
            )
            return

        view.progress_by_event[event.event_id] = status
        if status in {"not_started", "skipped"}:
            view.partial_by_event.pop(event.event_id, None)
        view.selected_event_id = event.event_id
        view.last_update = (event.event_name, status)
        view.refresh_items()
        await _edit_progress_message(interaction, view=view)


class FusionProgressShareModeView(discord.ui.View):
    """Ephemeral controls used to publish a manual progress share."""

    def __init__(
        self,
        *,
        user_id: int,
        target: fusion_sheets.FusionRow,
        events: Sequence[fusion_sheets.FusionEventRow],
        progress_by_event: Mapping[str, str],
        partial_by_event: Mapping[str, float] | None = None,
        traditional_prep: fusion_sheets.FusionTraditionalUserProgressRow | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.user_id = int(user_id)
        self.target = target
        self.events = list(events)
        self.progress_by_event = dict(progress_by_event)
        self.partial_by_event = dict(partial_by_event or {})
        self.traditional_prep = traditional_prep

    async def _handle_share(self, interaction: discord.Interaction, *, mode: Literal["summary", "detailed"]) -> None:
        if interaction.user.id != self.user_id:
            await _send_ephemeral(interaction, "This share panel belongs to a different user.")
            return

        client = getattr(interaction, "client", None)
        channel = None
        if client is not None:
            channel = await fusion_announcements.resolve_announcement_channel(client, self.target.announcement_channel_id)
        if channel is None:
            await _send_ephemeral(interaction, "Couldn’t find the fusion share channel right now.")
            return

        traditional_prep = self.traditional_prep
        if _is_traditional_fusion(self.target) and traditional_prep is None:
            try:
                traditional_prep = await fusion_sheets.get_user_traditional_progress(self.target.fusion_id, str(self.user_id))
            except Exception as exc:
                log.exception("fusion traditional prep failed to load for share", extra={"fusion_id": self.target.fusion_id, "user_id": self.user_id})
                await fusion_logs.send_ops_alert(component="traditional_prep", summary="share_load_failed", dedupe_key=f"fusion:traditional_prep:share:{self.target.fusion_id}", error=exc, fields={"fusion_id": self.target.fusion_id})
                await _send_ephemeral(interaction, "Couldn’t load champion preparation for sharing right now.")
                return

        share_embed = build_progress_share_embed(
            target=self.target,
            events=self.events,
            progress_by_event=self.progress_by_event,
            partial_by_event=self.partial_by_event,
            traditional_prep=traditional_prep,
            user_display_name=interaction.user.display_name,
            mode=mode,
        )
        try:
            await channel.send(embed=share_embed)
        except Exception as exc:
            context = {
                "fusion_id": self.target.fusion_id,
                "channel_id": self.target.announcement_channel_id,
                "user_id": self.user_id,
                "mode": mode,
            }
            log.exception("fusion progress share failed to send", extra=context)
            await fusion_logs.send_ops_alert(
                component="my_progress_share",
                summary="share_send_failed",
                dedupe_key=f"fusion:progress:share:{self.target.fusion_id}:{mode}",
                error=exc,
                fields=context,
            )
            await _send_ephemeral(interaction, "Couldn’t share progress right now. Try again shortly.")
            return

        await _send_ephemeral(interaction, f"Shared publicly ({_SHARE_MODE_LABELS[mode]}).")

    @discord.ui.button(
        label="Summary",
        style=discord.ButtonStyle.primary,
        custom_id=_FUSION_PROGRESS_SHARE_SUMMARY_CUSTOM_ID,
        row=0,
    )
    async def share_summary_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._handle_share(interaction, mode="summary")

    @discord.ui.button(
        label="Detailed",
        style=discord.ButtonStyle.secondary,
        custom_id=_FUSION_PROGRESS_SHARE_DETAILED_CUSTOM_ID,
        row=0,
    )
    async def share_detailed_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._handle_share(interaction, mode="detailed")




class _FusionProgressMarkAllButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="Mark All", style=discord.ButtonStyle.success, custom_id=_FUSION_PROGRESS_MARK_ALL_CUSTOM_ID, row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, FusionProgressPanelView):
            return
        if interaction.user.id != view.user_id:
            await _send_ephemeral(interaction, "This progress panel belongs to a different user.")
            return
        event = view.events_by_id.get(view.selected_event_id or "")
        if event is None or not event.milestones:
            await _send_ephemeral(interaction, "Selected event has no milestones.")
            return
        now = dt.datetime.now(dt.timezone.utc)
        await _safe_defer_progress_interaction(interaction)
        pending_updates: list[str] = []
        try:
            for milestone in event.milestones:
                milestone_key = str(milestone.points_needed)
                await fusion_sheets.upsert_user_event_progress(
                    view.fusion_id,
                    str(view.user_id),
                    event.event_id,
                    "done",
                    now,
                    milestone_key=milestone_key,
                )
                pending_updates.append(milestone_key)
        except Exception as exc:
            await _send_progress_save_failure(
                interaction,
                component="my_progress",
                dedupe_key=f"fusion:progress:mark_all:{view.fusion_id}:{event.event_id}",
                error=exc,
                fields={
                    "fusion_id": view.fusion_id,
                    "event_id": event.event_id,
                    "user_id": view.user_id,
                    "selected_status": "done",
                    "custom_id": _FUSION_PROGRESS_MARK_ALL_CUSTOM_ID,
                    "row_key": f"{view.fusion_id}|{view.user_id}|{event.event_id}|{pending_updates[-1] if pending_updates else ''}",
                },
            )
            return
        for milestone_key in pending_updates:
            view.progress_by_event[f"{event.event_id}:{milestone_key}"] = "done"
        view.last_update = (event.event_name, "done")
        await _edit_progress_message(interaction, view=view)

class _FusionProgressPageButton(discord.ui.Button):
    def __init__(self, *, direction: Literal["previous", "next"], disabled: bool) -> None:
        self.direction = direction
        label = "Previous" if direction == "previous" else "Next"
        custom_id = f"fusion:progress:page:{direction}"
        super().__init__(label=label, style=discord.ButtonStyle.secondary, custom_id=custom_id, row=2, disabled=disabled)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, FusionProgressPanelView):
            return
        if interaction.user.id != view.user_id:
            await _send_ephemeral(interaction, "This progress panel belongs to a different user.")
            return
        if self.direction == "previous":
            view.event_page_index = max(view.event_page_index - 1, 0)
        else:
            view.event_page_index = min(view.event_page_index + 1, view.event_page_count - 1)
        view.selected_event_id = view.first_event_id_on_current_page()
        view.refresh_items()
        await _edit_progress_message(interaction, view=view)


class _FusionProgressBackButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="Back", style=discord.ButtonStyle.secondary, custom_id=_FUSION_TRAD_BACK_CUSTOM_ID, row=3)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, FusionProgressPanelView):
            return
        await _edit_traditional_progress_choice_message(
            interaction,
            user_id=view.user_id,
            target=view.target,
            events=view.events,
            progress_by_event=view.progress_by_event,
            partial_by_event=view.partial_by_event,
        )


class _FusionProgressShareButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Share",
            style=discord.ButtonStyle.secondary,
            custom_id="fusion:progress:share",
            row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, (FusionProgressPanelView, TraditionalProgressChoiceView)):
            return
        share_view = FusionProgressShareModeView(
            user_id=view.user_id,
            target=view.target,
            events=view.events,
            progress_by_event=view.progress_by_event,
            partial_by_event=view.partial_by_event,
        )
        if interaction.response.is_done():
            await interaction.followup.send(
                "Choose a share mode to post your current progress publicly.",
                view=share_view,
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "Choose a share mode to post your current progress publicly.",
            view=share_view,
            ephemeral=True,
        )


class _FusionProgressUpdateButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="Log Partial Fragments", style=discord.ButtonStyle.primary, custom_id=_FUSION_PROGRESS_UPDATE_CUSTOM_ID, row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, FusionProgressPanelView):
            return
        event = view.events_by_id.get(view.selected_event_id or "")
        if event is None:
            await _send_ephemeral(interaction, "Choose an event first.")
            return
        status = view.progress_by_event.get(event.event_id, "not_started")
        if not _supports_partial_fragments(event, status=status):
            await _send_ephemeral(interaction, "Partial fragments can only be logged for in-progress fragment events with a reward amount.")
            return
        await interaction.response.send_modal(_FusionProgressModal(view=view, event=event))


class _FusionProgressModal(discord.ui.Modal, title="Log Partial Fragments"):
    partial_amount = discord.ui.TextInput(label="Fragments earned so far", placeholder="0", required=True)

    def __init__(self, *, view: "FusionProgressPanelView", event: fusion_sheets.FusionEventRow) -> None:
        super().__init__()
        self.panel_view = view
        self.event = event

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not self.event.milestones:
            await _send_ephemeral(
                interaction,
                "This event doesn’t support stepped progress yet, so partial fragments can’t be logged.",
            )
            return
        try:
            amount = float(str(self.partial_amount.value).strip())
        except ValueError:
            await _send_ephemeral(interaction, "Please enter a valid number.")
            return
        if amount < 0 or amount > self.event.reward_amount:
            await _send_ephemeral(interaction, f"Enter a value between 0 and {self.event.reward_amount:g}.")
            return
        now = dt.datetime.now(dt.timezone.utc)
        await _safe_defer_progress_interaction(interaction)
        try:
            await fusion_sheets.upsert_user_event_progress(
                self.panel_view.fusion_id,
                str(self.panel_view.user_id),
                self.event.event_id,
                "in_progress",
                now,
                partial_amount=amount,
            )
        except Exception as exc:
            await _send_progress_save_failure(
                interaction,
                component="my_progress",
                dedupe_key=f"fusion:progress:partial:{self.panel_view.fusion_id}:{self.event.event_id}",
                error=exc,
                fields={
                    "fusion_id": self.panel_view.fusion_id,
                    "event_id": self.event.event_id,
                    "user_id": self.panel_view.user_id,
                    "selected_status": "in_progress",
                    "custom_id": _FUSION_PROGRESS_UPDATE_CUSTOM_ID,
                    "partial_amount": amount,
                    "row_key": f"{self.panel_view.fusion_id}|{self.panel_view.user_id}|{self.event.event_id}|",
                },
            )
            return
        self.panel_view.progress_by_event[self.event.event_id] = "in_progress"
        self.panel_view.partial_by_event[self.event.event_id] = amount
        self.panel_view.last_update = (self.event.event_name, "in_progress")
        self.panel_view.refresh_items()
        await _edit_progress_message(interaction, view=self.panel_view)



def _build_traditional_progress_choice_embed(target: fusion_sheets.FusionRow) -> discord.Embed:
    embed = discord.Embed(
        title=f"My Progress: {target.fusion_name}",
        description="What do you want to track for this traditional fusion?",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Choices", value="Event/Tournament Progress\nChampion Preparation", inline=False)
    return embed


async def _edit_traditional_progress_choice_message(
    interaction: discord.Interaction,
    *,
    user_id: int,
    target: fusion_sheets.FusionRow,
    events: Sequence[fusion_sheets.FusionEventRow],
    progress_by_event: Mapping[str, str],
    partial_by_event: Mapping[str, float],
) -> None:
    view = TraditionalProgressChoiceView(
        user_id=user_id,
        target=target,
        events=events,
        progress_by_event=dict(progress_by_event),
        partial_by_event=dict(partial_by_event),
    )
    embed = _build_traditional_progress_choice_embed(target)
    if interaction.response.is_done():
        edit_original = getattr(interaction, "edit_original_response", None)
        if callable(edit_original):
            await edit_original(embed=embed, view=view)
            return
    await interaction.response.edit_message(embed=embed, view=view)


class TraditionalProgressChoiceView(discord.ui.View):
    def __init__(self, *, user_id: int, target: fusion_sheets.FusionRow, events: Sequence[fusion_sheets.FusionEventRow], progress_by_event: dict[str, str], partial_by_event: dict[str, float]) -> None:
        super().__init__(timeout=None)
        self.user_id = int(user_id)
        self.target = target
        self.events = list(events)
        self.progress_by_event = dict(progress_by_event)
        self.partial_by_event = dict(partial_by_event)
        self.add_item(_FusionProgressShareButton())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await _send_ephemeral(interaction, "This progress choice belongs to a different user.")
            return False
        return True

    @discord.ui.button(label="Event/Tournament Progress", style=discord.ButtonStyle.primary, custom_id=_FUSION_TRAD_CHOICE_EVENT_CUSTOM_ID)
    async def event_progress(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        view = FusionProgressPanelView(
            user_id=self.user_id,
            target=self.target,
            events=self.events,
            progress_by_event=self.progress_by_event,
            partial_by_event=self.partial_by_event,
            return_to_traditional_choice=True,
        )
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    @discord.ui.button(label="Champion Preparation", style=discord.ButtonStyle.secondary, custom_id=_FUSION_TRAD_CHOICE_PREP_CUSTOM_ID)
    async def champion_prep(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        try:
            prep = await fusion_sheets.get_user_traditional_progress(self.target.fusion_id, str(self.user_id))
        except Exception as exc:
            log.exception("fusion traditional prep failed to load", extra={"fusion_id": self.target.fusion_id, "user_id": self.user_id})
            await fusion_logs.send_ops_alert(component="traditional_prep", summary="load_failed", dedupe_key=f"fusion:traditional_prep:load:{self.target.fusion_id}", error=exc, fields={"fusion_id": self.target.fusion_id})
            await _send_ephemeral(interaction, "Couldn’t load champion preparation right now. Ask an admin to check the traditional progress tab config.")
            return
        view = TraditionalPrepPanelView(
            user_id=self.user_id,
            target=self.target,
            events=self.events,
            progress_by_event=self.progress_by_event,
            partial_by_event=self.partial_by_event,
            prep=prep,
        )
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class TraditionalPrepPanelView(discord.ui.View):
    def __init__(self, *, user_id: int, target: fusion_sheets.FusionRow, events: Sequence[fusion_sheets.FusionEventRow], progress_by_event: Mapping[str, str], partial_by_event: Mapping[str, float] | None = None, prep: fusion_sheets.FusionTraditionalUserProgressRow) -> None:
        super().__init__(timeout=None)
        self.user_id = int(user_id)
        self.target = target
        self.events = list(events)
        self.progress_by_event = dict(progress_by_event)
        self.partial_by_event = dict(partial_by_event or {})
        self.prep = prep
        self.add_item(_TraditionalPrepUpdateButton())
        self.add_item(_TraditionalPrepBackButton())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await _send_ephemeral(interaction, "This champion preparation panel belongs to a different user.")
            return False
        return True

    def build_embed(self) -> discord.Embed:
        return _build_traditional_prep_embed(target=self.target, events=self.events, progress_by_event=self.progress_by_event, prep=self.prep)


class _TraditionalPrepBackButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="Back", style=discord.ButtonStyle.secondary, custom_id=_FUSION_TRAD_BACK_CUSTOM_ID, row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, TraditionalPrepPanelView):
            return
        await _edit_traditional_progress_choice_message(
            interaction,
            user_id=view.user_id,
            target=view.target,
            events=view.events,
            progress_by_event=view.progress_by_event,
            partial_by_event=view.partial_by_event,
        )


class _TraditionalPrepUpdateButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="Update Champion Prep", style=discord.ButtonStyle.primary, custom_id=_FUSION_TRAD_PREP_UPDATE_CUSTOM_ID)

    async def callback(self, interaction: discord.Interaction) -> None:
        if not isinstance(self.view, TraditionalPrepPanelView):
            return
        await interaction.response.send_modal(_TraditionalPrepModal(view=self.view))


class _TraditionalPrepModal(discord.ui.Modal, title="Champion Preparation"):
    rares_level_40 = discord.ui.TextInput(label="Rares level 40", required=True, max_length=3)
    rares_ascended = discord.ui.TextInput(label="Rares fully ascended", required=True, max_length=3)
    epics_fused = discord.ui.TextInput(label="Epics fused", required=True, max_length=1)
    epics_level_50 = discord.ui.TextInput(label="Epics level 50", required=True, max_length=1)
    epics_ascended = discord.ui.TextInput(label="Epics fully ascended", required=True, max_length=1)

    def __init__(self, *, view: TraditionalPrepPanelView) -> None:
        super().__init__()
        self.panel_view = view
        self.rares_level_40.default = str(view.prep.rares_level_40)
        self.rares_ascended.default = str(view.prep.rares_ascended)
        self.epics_fused.default = str(view.prep.epics_fused)
        self.epics_level_50.default = str(view.prep.epics_level_50)
        self.epics_ascended.default = str(view.prep.epics_ascended)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            values = [int(str(item.value).strip()) for item in (self.rares_level_40, self.rares_ascended, self.epics_fused, self.epics_level_50, self.epics_ascended)]
        except ValueError:
            await _send_ephemeral(interaction, "Champion preparation counts must be whole numbers.")
            return
        needed_total = max(0, int(self.panel_view.target.needed))
        rares_acquired = self.panel_view.prep.rares_owned
        error = _validate_traditional_prep_counts(needed_total=needed_total, rares_acquired=rares_acquired, rares_level_40=values[0], rares_ascended=values[1], epics_fused=values[2], epics_level_50=values[3], epics_ascended=values[4])
        if error:
            await _send_ephemeral(interaction, error)
            return
        try:
            prep = await fusion_sheets.upsert_user_traditional_progress(self.panel_view.target.fusion_id, str(self.panel_view.user_id), rares_owned=self.panel_view.prep.rares_owned, rares_level_40=values[0], rares_ascended=values[1], epics_fused=values[2], epics_level_50=values[3], epics_ascended=values[4], target_ready=self.panel_view.prep.target_ready, updated_at=dt.datetime.now(dt.timezone.utc))
        except Exception as exc:
            log.exception("fusion traditional prep save failed", extra={"fusion_id": self.panel_view.target.fusion_id, "user_id": self.panel_view.user_id})
            await fusion_logs.send_ops_alert(component="traditional_prep", summary="save_failed", dedupe_key=f"fusion:traditional_prep:save:{self.panel_view.target.fusion_id}", error=exc, fields={"fusion_id": self.panel_view.target.fusion_id})
            await _send_ephemeral(interaction, "Couldn’t save champion preparation right now. Try again in a moment.")
            return
        self.panel_view.prep = prep
        await _edit_traditional_prep_message(interaction, view=self.panel_view)

async def _edit_traditional_prep_message(interaction: discord.Interaction, *, view: "TraditionalPrepPanelView") -> None:
    context = fusion_logs.interaction_context(interaction, custom_id=_FUSION_TRAD_PREP_UPDATE_CUSTOM_ID)
    context.update({
        "fusion_id": view.target.fusion_id,
        "user_id": view.user_id,
        "response_done_before_send": interaction.response.is_done(),
    })
    log.info("fusion traditional prep panel edit path selected", extra=context)
    await _safe_defer_progress_interaction(interaction)
    edit_original = getattr(interaction, "edit_original_response", None)
    if callable(edit_original):
        await edit_original(embed=view.build_embed(), view=view)
        return
    if not interaction.response.is_done():
        await interaction.response.edit_message(embed=view.build_embed(), view=view)
        return
    await interaction.followup.send(embed=view.build_embed(), view=view, ephemeral=True)


class FusionProgressPanelView(discord.ui.View):
    """Ephemeral progress panel for one user and one fusion."""

    def __init__(
        self,
        *,
        user_id: int,
        target: fusion_sheets.FusionRow,
        events: Sequence[fusion_sheets.FusionEventRow],
        progress_by_event: dict[str, str],
        partial_by_event: dict[str, float] | None = None,
        return_to_traditional_choice: bool = False,
    ) -> None:
        super().__init__(timeout=None)
        self.user_id = int(user_id)
        self.fusion_id = target.fusion_id
        self.target = target
        self.events = list(events)
        self.events_by_id = {event.event_id: event for event in self.events}
        self.progress_by_event = dict(progress_by_event)
        self.partial_by_event = dict(partial_by_event or {})
        self.return_to_traditional_choice = bool(return_to_traditional_choice)
        self.event_page_index = 0
        self.selected_event_id = self.events[0].event_id if self.events else None
        self.last_update: tuple[str, str] | None = None
        self.refresh_items()

    @property
    def ordered_events(self) -> list[fusion_sheets.FusionEventRow]:
        return _ordered_progress_events(self.events, self.progress_by_event)

    @property
    def event_page_count(self) -> int:
        if not self.events:
            return 1
        return max(1, (len(self.events) + _PROGRESS_EVENT_PAGE_SIZE - 1) // _PROGRESS_EVENT_PAGE_SIZE)

    def current_page_events(self) -> list[fusion_sheets.FusionEventRow]:
        ordered = self.ordered_events
        self.event_page_index = min(max(self.event_page_index, 0), self.event_page_count - 1)
        start = self.event_page_index * _PROGRESS_EVENT_PAGE_SIZE
        return ordered[start : start + _PROGRESS_EVENT_PAGE_SIZE]

    def first_event_id_on_current_page(self) -> str | None:
        current_events = self.current_page_events()
        return current_events[0].event_id if current_events else None

    def _coerce_selected_event_id(self) -> None:
        if not self.events:
            self.selected_event_id = None
            return
        current_event_ids = {event.event_id for event in self.current_page_events()}
        if self.selected_event_id in current_event_ids:
            return
        self.selected_event_id = self.first_event_id_on_current_page()

    def refresh_items(self) -> None:
        self.clear_items()
        if not self.events:
            return

        self._coerce_selected_event_id()
        current_events = self.current_page_events()
        self.add_item(
            _FusionProgressEventSelect(
                current_events,
                self.selected_event_id,
                self.progress_by_event,
                page_index=self.event_page_index,
                page_count=self.event_page_count,
            )
        )
        selected_status = None
        selected_event = None
        if self.selected_event_id:
            selected_status = self.progress_by_event.get(self.selected_event_id, "not_started")
            selected_event = self.events_by_id.get(self.selected_event_id)
            if selected_status == "done_bonus" and selected_event is not None and not _event_has_bonus(selected_event):
                selected_status = "done"
        self.add_item(_FusionProgressStatusSelect(selected_status, selected_event=selected_event))
        if _supports_partial_fragments(selected_event, status=selected_status):
            self.add_item(_FusionProgressUpdateButton())
        if selected_event is not None and selected_event.milestones:
            self.add_item(_FusionProgressMarkAllButton())
        if self.event_page_count > 1:
            self.add_item(_FusionProgressPageButton(direction="previous", disabled=self.event_page_index <= 0))
            self.add_item(_FusionProgressPageButton(direction="next", disabled=self.event_page_index >= self.event_page_count - 1))
        if not self.return_to_traditional_choice:
            self.add_item(_FusionProgressShareButton())
        if self.return_to_traditional_choice:
            self.add_item(_FusionProgressBackButton())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await _send_ephemeral(interaction, "This progress panel belongs to a different user.")
            return False
        return True

    def build_embed(self) -> discord.Embed:
        return _build_progress_summary_embed(
            target=self.target,
            events=self.events,
            progress_by_event=self.progress_by_event,
            partial_by_event=self.partial_by_event,
            selected_event_id=self.selected_event_id,
            last_update=self.last_update,
        )


def _progress_panel_diagnostics(
    interaction: discord.Interaction,
    *,
    target: fusion_sheets.FusionRow | None = None,
    events: Sequence[fusion_sheets.FusionEventRow] | None = None,
    view: "FusionProgressPanelView" | None = None,
    response_path: str,
    elapsed_ms: int,
) -> dict[str, object]:
    context = fusion_logs.interaction_context(interaction, custom_id=_FUSION_MY_PROGRESS_CUSTOM_ID)
    if target is not None:
        context["fusion_id"] = target.fusion_id
    if events is not None:
        context["event_count"] = len(events)
        context["event_page_size"] = _PROGRESS_EVENT_PAGE_SIZE
        context["event_page_count"] = max(1, (len(events) + _PROGRESS_EVENT_PAGE_SIZE - 1) // _PROGRESS_EVENT_PAGE_SIZE)
    if view is not None:
        context["component_count"] = len(view.children)
        context["event_options_visible"] = len(view.current_page_events())
        context["selected_event_id"] = view.selected_event_id or ""
        context["embed_field_count"] = len(view.build_embed().fields)
    context["response_path"] = response_path
    context["response_done_before_send"] = interaction.response.is_done()
    context["elapsed_ms"] = elapsed_ms
    return context


async def _send_my_progress_panel(
    interaction: discord.Interaction,
    *,
    target: fusion_sheets.FusionRow,
    events: Sequence[fusion_sheets.FusionEventRow],
    view: "FusionProgressPanelView",
    started_at: float,
) -> None:
    embed = view.build_embed()
    response_path = "followup_send_ephemeral" if interaction.response.is_done() else "direct_send_ephemeral"
    diagnostics = _progress_panel_diagnostics(
        interaction,
        target=target,
        events=events,
        view=view,
        response_path=response_path,
        elapsed_ms=int((time.monotonic() - started_at) * 1000),
    )
    log.info("fusion my-progress response path selected", extra=diagnostics)
    await _send_or_followup_ephemeral(interaction, embed=embed, view=view)


async def _handle_my_progress(interaction: discord.Interaction) -> None:
    started_at = time.monotonic()
    try:
        target = await fusion_sheets.get_publishable_fusion()
    except Exception as exc:
        context = fusion_logs.interaction_context(interaction, custom_id=_FUSION_MY_PROGRESS_CUSTOM_ID)
        log.exception("fusion my-progress failed to resolve active fusion", extra=context)
        await fusion_logs.send_ops_alert(
            component="my_progress",
            summary="resolve_active_fusion_failed",
            dedupe_key="fusion:my_progress:active",
            error=exc,
            fields=context,
        )
        await _send_ephemeral(interaction, "Couldn’t load fusion progress right now. Try again shortly.")
        return

    if target is None:
        await _send_ephemeral(interaction, "No active fusion is available right now.")
        return

    try:
        events = await fusion_sheets.get_fusion_events(target.fusion_id)
    except Exception as exc:
        context = fusion_logs.interaction_context(interaction, custom_id=_FUSION_MY_PROGRESS_CUSTOM_ID)
        context.update({"fusion_id": target.fusion_id})
        log.exception("fusion my-progress failed to load events", extra=context)
        await fusion_logs.send_ops_alert(
            component="my_progress",
            summary="load_events_failed",
            dedupe_key=f"fusion:my_progress:events:{target.fusion_id}",
            error=exc,
            fields=context,
        )
        await _send_ephemeral(interaction, "Couldn’t load events right now. Try again shortly.")
        return

    if not events:
        await _send_ephemeral(interaction, "No fusion events are configured yet.")
        return

    try:
        raw_progress = await fusion_sheets.get_user_event_progress(
            target.fusion_id,
            str(interaction.user.id),
        )
    except Exception as exc:
        context = fusion_logs.interaction_context(interaction, custom_id=_FUSION_MY_PROGRESS_CUSTOM_ID)
        context.update({"fusion_id": target.fusion_id})
        log.exception("fusion my-progress failed to load user progress", extra=context)
        await fusion_logs.send_ops_alert(
            component="my_progress",
            summary="load_user_progress_failed",
            dedupe_key=f"fusion:my_progress:user:{target.fusion_id}",
            error=exc,
            fields=context,
        )
        raw_progress = {}

    progress_by_event = _normalize_saved_progress(raw_progress=raw_progress, events=events)
    partial_by_event = _normalize_partial_payload(raw_progress)

    progress_by_event, malformed_payload = _normalize_progress_payload(progress_by_event)
    if malformed_payload:
        context = {"fusion_id": target.fusion_id, "user_id": interaction.user.id}
        log.warning("fusion my-progress payload malformed; continuing with empty state", extra=context)
        await fusion_logs.send_ops_alert(
            component="my_progress",
            summary="progress_payload_malformed",
            dedupe_key=f"fusion:my_progress:malformed:{target.fusion_id}",
            fields=context,
        )

    if _is_traditional_fusion(target):
        view = TraditionalProgressChoiceView(
            user_id=interaction.user.id,
            target=target,
            events=events,
            progress_by_event=progress_by_event,
            partial_by_event=partial_by_event,
        )
        embed = _build_traditional_progress_choice_embed(target)
        await _send_or_followup_ephemeral(interaction, embed=embed, view=view)
        return

    view = FusionProgressPanelView(
        user_id=interaction.user.id,
        target=target,
        events=events,
        progress_by_event=progress_by_event,
        partial_by_event=partial_by_event,
    )
    await _send_my_progress_panel(interaction, target=target, events=events, view=view, started_at=started_at)


async def _safe_defer_progress_interaction(interaction: discord.Interaction) -> None:
    response = getattr(interaction, "response", None)
    if response is None or response.is_done():
        return
    defer = getattr(response, "defer", None)
    if not callable(defer):
        return
    try:
        await defer(thinking=False)
    except Exception:
        log.debug("fusion progress interaction defer failed; continuing", exc_info=True)


async def _edit_progress_message(interaction: discord.Interaction, *, view: "FusionProgressPanelView") -> None:
    diagnostics = _progress_panel_diagnostics(
        interaction,
        target=view.target,
        events=view.events,
        view=view,
        response_path="edit_original_response" if interaction.response.is_done() else "response_edit_message",
        elapsed_ms=0,
    )
    log.info("fusion progress panel edit path selected", extra=diagnostics)
    if interaction.response.is_done():
        edit_original = getattr(interaction, "edit_original_response", None)
        if callable(edit_original):
            await edit_original(embed=view.build_embed(), view=view)
            return
    await interaction.response.edit_message(embed=view.build_embed(), view=view)


async def _send_progress_save_failure(
    interaction: discord.Interaction,
    *,
    component: str,
    dedupe_key: str,
    error: BaseException,
    fields: dict[str, object],
) -> None:
    diagnostics = await fusion_sheets.get_progress_sheet_diagnostics()
    fields.update(diagnostics)
    fields.setdefault("save_success", False)
    fields.setdefault("save_failure_reason", str(error) or type(error).__name__)
    log.exception("fusion progress save failed", extra=fields)
    await fusion_logs.send_ops_alert(
        component=component,
        summary="progress_save_failed",
        dedupe_key=dedupe_key,
        error=error,
        fields=fields,
    )
    await _send_ephemeral(interaction, "Couldn’t save progress right now. Try again in a moment.")


def _normalize_saved_progress(
    *,
    raw_progress: object,
    events: Sequence[fusion_sheets.FusionEventRow],
) -> dict[str, str]:
    """Normalize saved payloads into per-event statuses with safe defaults."""

    normalized: dict[str, str] = {}
    known_event_ids = {event.event_id for event in events}
    progress_payload: object = raw_progress
    if isinstance(raw_progress, dict) and "progress" in raw_progress:
        nested = raw_progress.get("progress")
        if isinstance(nested, dict):
            progress_payload = nested
        else:
            progress_payload = {}

    if isinstance(progress_payload, dict):
        for event_id, status in progress_payload.items():
            event_token = str(event_id or "").strip()
            if event_token not in known_event_ids:
                continue
            status_token = str(status or "").strip().lower()
            normalized[event_token] = status_token if status_token in _ALLOWED_PROGRESS_STATES else "not_started"

    return normalized


class FusionOptInView(discord.ui.View):
    """Persistent button view for fusion role management and private progress."""

    def __init__(self, *, include_opt_buttons: bool = True) -> None:
        super().__init__(timeout=None)
        if not include_opt_buttons:
            self.remove_item(self.opt_in_button)
            self.remove_item(self.opt_out_button)

    @discord.ui.button(
        label="Opt In",
        style=discord.ButtonStyle.success,
        custom_id=_FUSION_OPT_IN_CUSTOM_ID,
        row=0,
    )
    async def opt_in_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await _handle_opt_action(interaction, action="in")

    @discord.ui.button(
        label="Opt Out",
        style=discord.ButtonStyle.secondary,
        custom_id=_FUSION_OPT_OUT_CUSTOM_ID,
        row=0,
    )
    async def opt_out_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await _handle_opt_action(interaction, action="out")

    @discord.ui.button(
        label="My Progress",
        style=discord.ButtonStyle.primary,
        custom_id=_FUSION_MY_PROGRESS_CUSTOM_ID,
        row=0,
    )
    async def my_progress_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await _handle_my_progress(interaction)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        context = fusion_logs.interaction_context(interaction, custom_id=getattr(item, "custom_id", None))
        log.exception("fusion opt-in view interaction failed", extra=context)
        await fusion_logs.send_ops_alert(
            component="interaction",
            summary="view_handler_failed",
            dedupe_key=f"fusion:view_error:{context.get('custom_id')}",
            error=error,
            fields=context,
        )
        await _send_ephemeral(interaction, "Temporary issue. Try again shortly.")


def build_fusion_opt_in_view(target: fusion_sheets.FusionRow) -> discord.ui.View:
    """Build the reusable fusion button row for announcement and reminders."""

    return FusionOptInView(include_opt_buttons=target.opt_in_role_id is not None)


def register_persistent_fusion_views(bot: commands.Bot) -> None:
    """Register persistent fusion button handlers on startup."""

    bot.add_view(FusionOptInView())


__all__ = [
    "FusionOptInView",
    "build_fusion_opt_in_view",
    "register_persistent_fusion_views",
]
