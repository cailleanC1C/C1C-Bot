"""Runtime repair for the Captain's Table control-center path.

PR #1137 proved that the control-center wrapper is reached in production, but it
could fail before rendering when a newly-created qualification tiebreak match was
written inside a Sheet read scope and ``record_thread_id`` immediately re-read the
cached pre-write MATCHES table.

This repair keeps the existing workflow, but makes the write/read boundary safe:
- persist a known tiebreak thread ID directly to its existing MATCHES row without
  re-reading MATCHES;
- recover the already-created live tiebreak row whose thread_id is still blank;
- keep tiebreak thread result controls best-effort and restart-safe;
- render Captain's Table through the known PartialMessage target rather than a
  second Discord fetch;
- preserve useful control-center state even if Discord thread publication has a
  transient failure, and log the concrete exception type/message.
"""

from __future__ import annotations

import logging

import discord

from shared.sheets.async_core import acall_with_backoff, aget_worksheet

from modules.community.live_arena import captains_table_control_center as control
from modules.community.live_arena import knockout
from modules.community.live_arena.messages import load_messages, load_pr5_config
from modules.community.live_arena.qualification import MATCH_HEADERS
from modules.community.live_arena.registration import RegistrationError, _locks
from modules.community.live_arena.service import _text

log = logging.getLogger("c1c.community.live_arena.captains_table_runtime_repair")
_installed = False


def _match_row_number(state: control.ControlState, match_id: str) -> int:
    matches = [
        index
        for index, row in enumerate(state.matches, 2)
        if _text(row.get("match_id")) == str(match_id)
    ]
    if len(matches) != 1:
        raise RegistrationError(
            f"Qualification tiebreak match {match_id} could not be resolved in the current tournament state"
        )
    return matches[0]


async def _persist_thread_id_without_reread(
    service: knockout.KnockoutService,
    state: control.ControlState,
    match: dict[str, object],
    thread_id: str,
) -> None:
    """Write only MATCHES.thread_id using the already-authoritative in-memory row.

    Do not call ``repository.matches()`` here. During startup/panel reconciliation
    the logical operation can still be inside ``sheet_read_scope`` and another read
    would return the pre-write MATCHES matrix that caused the production failure.
    """

    match_id = _text(match.get("match_id"))
    if not match_id:
        raise RegistrationError("Qualification tiebreak match is missing match_id")

    async with _locks[(service.sheet_id, state.tournament_id)]:
        row_number = _match_row_number(state, match_id)
        column_number = MATCH_HEADERS.index("thread_id") + 1
        tab = _text(service.repository.config.get("MATCHES_TAB"))
        if not tab:
            raise RegistrationError("Live Arena MATCHES table is not configured")
        worksheet = await aget_worksheet(service.sheet_id, tab)
        await acall_with_backoff(
            worksheet.update_cell,
            row_number,
            column_number,
            str(thread_id),
        )

        # Write-through the state carried by this reconciliation so later helpers
        # in the same read scope never need a Sheet read to observe the new ID.
        match["thread_id"] = str(thread_id)
        for row in state.matches:
            if _text(row.get("match_id")) == match_id:
                row["thread_id"] = str(thread_id)
        for row in state.tiebreak_matches:
            if _text(row.get("match_id")) == match_id:
                row["thread_id"] = str(thread_id)


async def _resolve_thread(bot, thread_id: str):
    thread = bot.get_channel(int(thread_id))
    if thread is None:
        thread = await bot.fetch_channel(int(thread_id))
    return thread


async def _ensure_result_controls(manager, thread_id: str) -> None:
    """Best-effort repair of the normal result controls on a tiebreak starter."""

    from modules.community.live_arena import result_views

    try:
        thread = await _resolve_thread(manager.bot, thread_id)
        get_partial = getattr(thread, "get_partial_message", None)
        starter = (
            get_partial(int(thread_id))
            if callable(get_partial)
            else await thread.fetch_message(int(thread_id))
        )
        await starter.edit(view=result_views.MatchResultView(str(manager.sheet_id)))
    except Exception as exc:
        # The authoritative thread ID is already persisted. Do not delete a valid
        # matchup because a cosmetic/control refresh had a transient Discord error.
        log.exception(
            "Live Arena tiebreak result-control refresh failed • thread=%s • error=%s: %s",
            thread_id,
            type(exc).__name__,
            exc,
        )


async def _publish_tiebreak_threads(
    manager,
    service: knockout.KnockoutService,
    state: control.ControlState,
    templates,
) -> None:
    if not state.tiebreak_required or state.unsupported_tie:
        return

    from modules.community.live_arena import qualification_panel

    config = service.repository.config
    forum = await qualification_panel._resolve_channel(
        manager.bot, int(config["MATCH_FORUM_CHANNEL_ID"])
    )

    for match in state.tiebreak_matches:
        existing_thread_id = _text(match.get("thread_id"))
        if existing_thread_id:
            await _ensure_result_controls(manager, existing_thread_id)
            continue

        embed = templates["qualification_tiebreak_thread"].embed(
            player_a_mention=f"<@{_text(match['player_a_discord_user_id'])}>",
            player_b_mention=f"<@{_text(match['player_b_discord_user_id'])}>",
        )
        created = await forum.create_thread(
            name=(
                f"Qualification Tiebreak • {_text(match['player_a_display_name'])} vs "
                f"{_text(match['player_b_display_name'])}"
            )[:100],
            content=(
                f"<@{_text(match['player_a_discord_user_id'])}> "
                f"<@{_text(match['player_b_discord_user_id'])}>"
            ),
            embed=embed,
            allowed_mentions=discord.AllowedMentions(
                users=True, roles=False, everyone=False
            ),
        )
        thread = getattr(created, "thread", None)
        if thread is None and isinstance(created, tuple):
            thread = created[0]
        if thread is None:
            thread = created

        try:
            await _persist_thread_id_without_reread(
                service, state, match, str(thread.id)
            )
        except Exception as exc:
            try:
                await thread.delete(
                    reason="Live Arena tiebreak thread ID persistence failed"
                )
            except Exception:
                log.exception(
                    "Live Arena untracked tiebreak thread cleanup failed • thread=%s",
                    getattr(thread, "id", "unknown"),
                )
            log.exception(
                "Live Arena tiebreak thread persistence failed • match=%s • error=%s: %s",
                _text(match.get("match_id")),
                type(exc).__name__,
                exc,
            )
            raise

        await _ensure_result_controls(manager, str(thread.id))


async def _ensure_tiebreak_flow(manager) -> control.ControlState:
    """Hydrate the current tiebreak while keeping in-memory state authoritative."""

    service = knockout.KnockoutService(manager.sheet_id)
    await service.initialize()
    templates = await control._load_templates(manager.sheet_id)
    state = await control._ensure_tiebreak_sheet_state(service)

    # Publish state to the manager before touching Discord. If Discord publication
    # fails, Captain's Table can still explain what is required on the next render.
    manager._qualification_tiebreak_state = state
    manager._qualification_tiebreak_required = (
        state.tiebreak_required and not state.tiebreak_complete
    )

    try:
        if state.tiebreak_required and not state.unsupported_tie:
            await _publish_tiebreak_threads(manager, service, state, templates)
            resolved = await control._materialize_tiebreak_resolutions(service, state)
            if state.tiebreak_complete and resolved:
                state.tiebreak_resolved = True
        manager._qualification_tiebreak_required = (
            state.tiebreak_required and not state.tiebreak_complete
        )
        manager._qualification_tiebreak_state = state
        return state
    except Exception as exc:
        log.exception(
            "Live Arena qualification tiebreak reconciliation failed • tournament=%s • error=%s: %s",
            state.tournament_id,
            type(exc).__name__,
            exc,
        )
        raise


def _add_control_fields(embed: discord.Embed, state: control.ControlState, templates) -> None:
    stage, current_step, next_step = control._stage_summary(state)
    title, description = templates["organizer_control_stage"].render(
        stage=stage,
        current_step=current_step,
        next_step=next_step,
    )
    embed.add_field(
        name=title or "Current tournament state", value=description, inline=False
    )

    attention = control._attention_lines(state)
    if attention:
        title, description = templates["organizer_control_attention"].render(
            attention_lines="\n".join(attention)
        )
        embed.add_field(
            name=title or "Attention needed", value=description, inline=False
        )

    title, description = templates["organizer_control_progress"].render(
        progress_lines=control._progress_lines(state)
    )
    embed.add_field(
        name=title or "Tournament progress", value=description, inline=False
    )

    if state.standings:
        title, description = templates["organizer_control_standings"].render(
            standings_lines=control._standings_lines(state)
        )
        embed.add_field(
            name=title or "Current qualification order",
            value=description,
            inline=False,
        )


async def _render_control_center(manager, state: control.ControlState) -> None:
    """Render directly to the configured panel message without fetching it first."""

    try:
        config, _ = await load_pr5_config(manager.sheet_id)
        message_id = _text(config.get("ORGANIZER_PANEL_MESSAGE_ID"))
        if not message_id:
            return

        channel = manager.bot.get_channel(int(config["ORGANIZER_CHANNEL_ID"]))
        if channel is None:
            channel = await manager.bot.fetch_channel(
                int(config["ORGANIZER_CHANNEL_ID"])
            )

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
        _add_control_fields(embed, state, templates)

        get_partial = getattr(channel, "get_partial_message", None)
        message = (
            get_partial(int(message_id))
            if callable(get_partial)
            else await channel.fetch_message(int(message_id))
        )
        await message.edit(embed=embed, view=manager.view(tournament.status))
        log.info(
            "Live Arena Captain's Table control-center refreshed • message=%s • tournament=%s • tiebreak_required=%s",
            message_id,
            state.tournament_id,
            state.tiebreak_required and not state.tiebreak_complete,
        )
    except Exception as exc:
        log.exception(
            "Live Arena Captain's Table control-center render failed • tournament=%s • error=%s: %s",
            state.tournament_id,
            type(exc).__name__,
            exc,
        )
        raise


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    # The #1137 sync wrapper resolves these functions from the module globals at
    # call time, so replacing them here fixes the deployed final runtime path
    # without adding yet another OrganizerPanelManager.sync wrapper.
    control._publish_tiebreak_threads = _publish_tiebreak_threads
    control._ensure_tiebreak_flow = _ensure_tiebreak_flow
    control._render_control_center = _render_control_center
