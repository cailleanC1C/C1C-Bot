"""Authoritative final Captain's Table boundary for round close and tournament completion.

This module runs after the existing Live Arena UX layers.  It owns the last visible
Captain's Table edit so stale qualification-era pruning cannot remove the action that
matches the current Sheet state.

It also gives knockout standings an explicit Top 8 title and stamps the Captain's
Table with a Sheet-owned Last updated footer/timestamp on every final render.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from modules.community.live_arena.service import _text

log = logging.getLogger("c1c.community.live_arena.final_tournament_close_boundary")
_installed = False

_KNOCKOUT_STAGES = {"quarterfinal", "semifinal", "final"}
_NORMAL_ROUND_STAGES = {"qualification", *_KNOCKOUT_STAGES}
_OPEN_STATUSES = {
    "active",
    "published",
    "open",
    "published/open",
    "ready_to_close",
    "correction_in_progress",
}
_CLOSABLE_STATUSES = {"ready_to_close", "correction_in_progress"}
_PREVIEW_STATUSES = {"preview", "approved", "proposed"}
_PROGRESSION_ACTIONS = {
    "Close Current Round",
    "Complete Tournament",
    "Freeze Top 8",
    "Record BO3 Tiebreak",
    "Approve & Open Knockout",
}

_COPY_CONTRACTS = {
    "organizer_control_knockout_standings": {"standings_lines"},
    "organizer_control_last_updated": set(),
}


def _round_sort_key(row) -> tuple[int, int]:
    stage = _text(row.get("round_stage")).lower()
    order = {"qualification": 1, "quarterfinal": 2, "semifinal": 3, "final": 4}
    try:
        number = int(_text(row.get("round_number")) or 0)
    except ValueError:
        number = 0
    return order.get(stage, 0), number


def _round_for_stage(state, stage: str):
    found = [
        row
        for row in state.rounds
        if _text(row.get("tournament_id")) == state.tournament_id
        and _text(row.get("round_stage")).lower() == stage
    ]
    if not found:
        return None
    return max(found, key=_round_sort_key)


def _current_normal_round(state):
    found = [
        row
        for row in state.rounds
        if _text(row.get("tournament_id")) == state.tournament_id
        and _text(row.get("round_stage")).lower() in _NORMAL_ROUND_STAGES
        and _text(row.get("status")).lower() in _OPEN_STATUSES
    ]
    return max(found, key=_round_sort_key) if found else None


def _current_knockout_preview(state):
    found = [
        row
        for row in state.rounds
        if _text(row.get("tournament_id")) == state.tournament_id
        and _text(row.get("round_stage")).lower() in _KNOCKOUT_STAGES
        and _text(row.get("status")).lower() in _PREVIEW_STATUSES
    ]
    return max(found, key=_round_sort_key) if found else None


def _has_knockout_round(state) -> bool:
    return any(
        _text(row.get("tournament_id")) == state.tournament_id
        and _text(row.get("round_stage")).lower() in _KNOCKOUT_STAGES
        for row in state.rounds
    )


def _base_allowed_actions(manager, tournament_status: str) -> set[str]:
    allowed = set(getattr(manager, "_captains_table_allowed", None) or ())
    if allowed:
        return allowed

    from modules.community.live_arena import captains_table_quota_safe

    return set(captains_table_quota_safe._safe_panel_actions(manager, tournament_status))


def _authoritative_actions(manager, state, tournament_status: str) -> set[str]:
    """Correct only progression controls from the same state used by the embed.

    Existing maintenance/roster/review controls are preserved.  Progression controls
    are then rebuilt from authoritative round state so the visible action cannot be
    stale after a result, round close, or preview creation.
    """

    status = _text(tournament_status).lower()
    allowed = _base_allowed_actions(manager, status)

    if status != "active":
        allowed.discard("Close Current Round")
        allowed.discard("Complete Tournament")
        return allowed

    q3 = _round_for_stage(state, "qualification")
    q3_rows = [
        row
        for row in state.rounds
        if _text(row.get("tournament_id")) == state.tournament_id
        and _text(row.get("round_stage")).lower() == "qualification"
        and _text(row.get("round_number")) == "3"
    ]
    q3 = q3_rows[0] if len(q3_rows) == 1 else q3

    from modules.community.live_arena import knockout

    seed = knockout._seed_row(state.rounds, state.tournament_id)

    # Q3 completion has its own explicit gate. A qualification tiebreak is not a
    # normal round-close action, so keep exactly the known next organizer decision.
    if q3 is not None and _text(q3.get("status")).lower() == "closed" and seed is None:
        allowed.difference_update(_PROGRESSION_ACTIONS)
        if getattr(state, "unsupported_tie", False):
            return allowed
        if getattr(state, "tiebreak_required", False) and not getattr(
            state, "tiebreak_complete", False
        ):
            allowed.add("Record BO3 Tiebreak")
        else:
            allowed.add("Freeze Top 8")
        return allowed

    current = _current_normal_round(state)
    if current is not None:
        # A live round owns progression. Only a genuinely closable round may show
        # Finish Round; in-progress rounds must not expose it early.
        allowed.discard("Complete Tournament")
        allowed.discard("Approve & Open Knockout")
        if _text(current.get("status")).lower() in _CLOSABLE_STATUSES:
            allowed.add("Close Current Round")
        else:
            allowed.discard("Close Current Round")
        return allowed

    preview = _current_knockout_preview(state)
    if preview is not None:
        # QF -> SF and SF -> Final must never dead-end after Finish Round.
        allowed.discard("Close Current Round")
        allowed.discard("Complete Tournament")
        allowed.add("Approve & Open Knockout")
        return allowed

    final = _round_for_stage(state, "final")
    if final is not None and _text(final.get("status")).lower() == "closed":
        # This is the exact post-Final-close state required by KnockoutService.
        allowed.discard("Close Current Round")
        allowed.discard("Approve & Open Knockout")
        allowed.add("Complete Tournament")
        return allowed

    allowed.discard("Close Current Round")
    allowed.discard("Complete Tournament")
    return allowed


def _replace_knockout_standings_title(embed, state, templates) -> None:
    if not getattr(state, "standings", None) or not _has_knockout_round(state):
        return
    if not getattr(embed, "fields", None):
        return

    from modules.community.live_arena import captains_table_control_center as control

    title, description = templates["organizer_control_knockout_standings"].render(
        standings_lines=control._standings_lines(state)
    )
    index = len(embed.fields) - 1
    embed.set_field_at(
        index,
        name=title or "Top 8 standings",
        value=description,
        inline=False,
    )


def _stamp_control_center(embed, templates, *, now: datetime | None = None) -> None:
    title, _ = templates["organizer_control_last_updated"].render()
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    else:
        moment = moment.astimezone(UTC)
    embed.set_footer(text=title)
    embed.timestamp = moment


async def _render_control_center(manager, state) -> None:
    """Write one final authoritative Captain's Table payload and action set."""

    from modules.community.live_arena import captains_table_control_center as control
    from modules.community.live_arena import captains_table_runtime_repair as runtime_repair
    from modules.community.live_arena import full_set_scoring
    from modules.community.live_arena.messages import load_messages, load_pr5_config

    try:
        config, _ = await load_pr5_config(manager.sheet_id)
        message_id = _text(config.get("ORGANIZER_PANEL_MESSAGE_ID"))
        if not message_id:
            return

        channel = manager.bot.get_channel(int(config["ORGANIZER_CHANNEL_ID"]))
        if channel is None:
            channel = await manager.bot.fetch_channel(int(config["ORGANIZER_CHANNEL_ID"]))

        config, tournament, _participants, counts, _parity = await manager.data(
            getattr(channel, "guild", None)
        )
        templates = await control._load_templates(manager.sheet_id)
        base_messages = await load_messages(
            manager.sheet_id,
            config["MESSAGES_TAB"],
            {"organizer_panel"},
        )
        embed = base_messages["organizer_panel"].embed(
            tournament_name=tournament.tournament_name,
            status=_text(tournament.status).replace("_", " ").title(),
            confirmed_count=counts["confirmed"],
            max_participants=tournament.max_participants,
        )
        embed.clear_fields()
        runtime_repair._add_control_fields(embed, state, templates)
        _replace_knockout_standings_title(embed, state, templates)
        _stamp_control_center(embed, templates)

        allowed = _authoritative_actions(manager, state, tournament.status)
        manager._captains_table_allowed = set(allowed)

        # Do not trust another wrapper to keep the final action. Build the raw view,
        # then prune it once here from the exact same authoritative state as the
        # embed. This is the final visible boundary.
        view = manager.view(tournament.status)
        view = full_set_scoring._finalize_visible_view(view, set(allowed))

        get_partial = getattr(channel, "get_partial_message", None)
        message = (
            get_partial(int(message_id))
            if callable(get_partial)
            else await channel.fetch_message(int(message_id))
        )
        await message.edit(embed=embed, view=view)
        log.info(
            "Live Arena final Captain's Table boundary refreshed • tournament=%s • actions=%s",
            state.tournament_id,
            sorted(allowed),
        )
    except Exception as exc:
        log.exception(
            "Live Arena final Captain's Table boundary failed • tournament=%s • error=%s: %s",
            getattr(state, "tournament_id", ""),
            type(exc).__name__,
            exc,
        )
        raise


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import captains_table_control_center as control

    # Extend the existing Sheet-owned copy contract before the first runtime render.
    control._COPY_CONTRACTS.update(_COPY_CONTRACTS)

    # Own the final visible render directly. This supersedes the earlier Q3-only
    # action-state wrapper rather than stacking another manager.sync decorator.
    control._render_control_center = _render_control_center
