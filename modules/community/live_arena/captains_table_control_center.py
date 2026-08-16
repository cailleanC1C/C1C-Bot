"""Final Captain's Table control-center UX and playable qualification tiebreaks.

The organizer panel should answer four questions without requiring button hunting:
where the tournament is, what needs attention, what has completed, and what happens
next. Qualification tiebreaks are real tournament matchups, not ID-entry forms.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import discord

from modules.community.live_arena import knockout, knockout_tiebreak
from modules.community.live_arena.competition import MATCH_TERMINAL_STATUSES
from modules.community.live_arena.messages import load_messages, load_pr5_config
from modules.community.live_arena.organizer_panel import OrganizerView
from modules.community.live_arena.qualification import (
    MATCH_HEADERS,
    ROUND_HEADERS,
    QualificationService,
)
from modules.community.live_arena.registration import RegistrationError, _locks, utc_iso
from modules.community.live_arena.service import _text
from modules.community.live_arena.views import error_embed

log = logging.getLogger("c1c.community.live_arena.captains_table_control_center")
_installed = False

_TB_ROUND_SUFFIX = "TB"
_TB_STAGE = "qualification_tiebreak"

_COPY_CONTRACTS = {
    "organizer_control_stage": {"stage", "current_step", "next_step"},
    "organizer_control_attention": {"attention_lines"},
    "organizer_control_progress": {"progress_lines"},
    "organizer_control_standings": {"standings_lines"},
    "qualification_tiebreak_thread": {"player_a_mention", "player_b_mention"},
    "qualification_tiebreak_open": {"players", "state", "thread_links"},
    "qualification_tiebreak_not_required": set(),
}


@dataclass
class ControlState:
    tournament_id: str
    rounds: list[dict[str, object]]
    matches: list[dict[str, object]]
    standings: list
    tie_groups: list[list[str]]
    tiebreak_matches: list[dict[str, object]]
    tiebreak_resolved: bool
    unsupported_tie: bool = False

    @property
    def tiebreak_required(self) -> bool:
        return bool(self.tie_groups)

    @property
    def tiebreak_complete(self) -> bool:
        if not self.tiebreak_required or self.unsupported_tie:
            return False
        return bool(self.tiebreak_matches) and all(
            _text(row.get("status")) in MATCH_TERMINAL_STATUSES
            and _text(row.get("final_winner_discord_user_id"))
            for row in self.tiebreak_matches
        )


def _round(rounds, tid: str, *, stage: str | None = None, number: int | None = None):
    found = []
    for row in rounds:
        if _text(row.get("tournament_id")) != tid:
            continue
        if stage is not None and _text(row.get("round_stage")).lower() != stage:
            continue
        if number is not None and _text(row.get("round_number")) != str(number):
            continue
        found.append(row)
    return found[0] if len(found) == 1 else None


def _tie_groups(standings, affected) -> list[list[str]]:
    by_rank: dict[int, list[str]] = {}
    for entry in affected:
        by_rank.setdefault(int(entry.rank), []).append(entry.discord_user_id)
    order = {entry.discord_user_id: index for index, entry in enumerate(standings)}
    groups = [ids for ids in by_rank.values() if len(ids) >= 2]
    for group in groups:
        group.sort(key=lambda uid: order.get(uid, 10_000))
    return groups


def _tb_round_id(tid: str) -> str:
    return f"{tid}-{_TB_ROUND_SUFFIX}"


def _tb_match_id(tid: str, index: int) -> str:
    return f"{tid}-{_TB_ROUND_SUFFIX}-M{index:02d}"


def _new_round(tid: str, now: str) -> dict[str, object]:
    row = {header: "" for header in ROUND_HEADERS}
    row.update(
        tournament_id=tid,
        round_id=_tb_round_id(tid),
        round_name="Qualification Tiebreak",
        round_stage=_TB_STAGE,
        round_number="4",
        status="active",
        opens_at_utc=now,
        published_at_utc=now,
        generated_at_utc=now,
        generated_by_discord_user_id="system",
        notes="Automatic qualification-order tiebreak matchups.",
    )
    return row


def _new_match(tid: str, index: int, a, b, now: str) -> dict[str, object]:
    row = {header: "" for header in MATCH_HEADERS}
    row.update(
        tournament_id=tid,
        round_id=_tb_round_id(tid),
        match_id=_tb_match_id(tid, index),
        match_number=str(index),
        player_a_discord_user_id=a.discord_user_id,
        player_a_display_name=a.display_name,
        player_b_discord_user_id=b.discord_user_id,
        player_b_display_name=b.display_name,
        status="published",
        has_scheduling_conflict="false",
        published_at_utc=now,
        notes="Qualification-order tiebreak. First player to win 2 games wins.",
    )
    return row


async def _ensure_tiebreak_sheet_state(service: knockout.KnockoutService) -> ControlState:
    config = await knockout.load_config(service.sheet_id)
    tid = config["ACTIVE_TOURNAMENT_ID"]
    rounds = [dict(row) for row in await service.repository.rounds()]
    matches = [dict(row) for row in await service.repository.matches()]
    standings = knockout.calculate_qualification_standings(matches, tid)
    affected = knockout._competitive_ties(standings[:8], standings)
    groups = _tie_groups(standings, affected)
    unsupported = any(len(group) != 2 for group in groups)

    q3 = knockout._round_by_id(rounds, tid, f"{tid}-Q3")
    if not groups or q3 is None or _text(q3.get("status")) != "closed":
        return ControlState(tid, rounds, matches, standings, groups, [], False, unsupported)

    tb_round = knockout._round_by_id(rounds, tid, _tb_round_id(tid))
    tb_matches = [row for row in matches if _text(row.get("round_id")) == _tb_round_id(tid)]
    if unsupported:
        return ControlState(tid, rounds, matches, standings, groups, tb_matches, False, True)

    by_uid = {entry.discord_user_id: entry for entry in standings}
    changed = False
    now = utc_iso(service.clock().astimezone(UTC))
    if tb_round is None:
        tb_round = _new_round(tid, now)
        rounds.append(tb_round)
        changed = True

    existing_by_pair = {
        frozenset(
            {
                _text(row.get("player_a_discord_user_id")),
                _text(row.get("player_b_discord_user_id")),
            }
        ): row
        for row in tb_matches
    }
    for index, group in enumerate(groups, 1):
        pair = frozenset(group)
        if pair in existing_by_pair:
            continue
        a, b = (by_uid[group[0]], by_uid[group[1]])
        match = _new_match(tid, index, a, b, now)
        matches.append(match)
        tb_matches.append(match)
        changed = True

    if changed:
        async with _locks[(service.sheet_id, tid)]:
            old_rounds = await service.repository.rounds()
            old_matches = await service.repository.matches()
            # Reconcile by stable IDs so a concurrent startup pass cannot append the same rows twice.
            round_ids = {_text(row.get("round_id")) for row in old_rounds}
            merged_rounds = [dict(row) for row in old_rounds]
            for row in rounds:
                if _text(row.get("round_id")) not in round_ids:
                    merged_rounds.append(dict(row))
                    round_ids.add(_text(row.get("round_id")))
            match_ids = {_text(row.get("match_id")) for row in old_matches}
            merged_matches = [dict(row) for row in old_matches]
            for row in matches:
                if _text(row.get("match_id")) not in match_ids:
                    merged_matches.append(dict(row))
                    match_ids.add(_text(row.get("match_id")))
            await service.repository.persist_state(
                merged_rounds,
                merged_matches,
                previous_rounds=old_rounds,
                previous_matches=old_matches,
            )
            rounds, matches = merged_rounds, merged_matches
            tb_matches = [row for row in matches if _text(row.get("round_id")) == _tb_round_id(tid)]

    return ControlState(tid, rounds, matches, standings, groups, tb_matches, False, False)


async def _materialize_tiebreak_resolutions(service: knockout.KnockoutService, state: ControlState) -> bool:
    if not state.tiebreak_complete:
        return False

    row = knockout_tiebreak._tiebreak_row(await service.repository.rounds(), state.tournament_id)
    existing = knockout_tiebreak._read_resolutions(row)
    resolved_groups = {tuple(sorted(item.get("group", []))) for item in existing}
    changed = False

    for match in state.tiebreak_matches:
        group = sorted(
            [
                _text(match.get("player_a_discord_user_id")),
                _text(match.get("player_b_discord_user_id")),
            ]
        )
        key = tuple(group)
        if key in resolved_groups:
            continue
        winner = _text(match.get("final_winner_discord_user_id"))
        if not winner:
            return False
        loser = group[0] if group[1] == winner else group[1]
        await knockout_tiebreak.record_tiebreak_resolution(
            service,
            "system",
            [winner, loser],
            "Recorded from the completed qualification tiebreak matchup.",
        )
        resolved_groups.add(key)
        changed = True

    return changed or bool(resolved_groups)


async def _load_templates(sheet_id: str):
    from modules.community.live_arena import messages

    # These are final-stage UX contracts. Extend the generic loader rather than
    # hardcoding visible copy in Python; the actual wording lives in MESSAGES.
    messages.MESSAGE_CONTRACTS.update(_COPY_CONTRACTS)
    config, _ = await load_pr5_config(sheet_id)
    return await load_messages(sheet_id, config["MESSAGES_TAB"], set(_COPY_CONTRACTS))


async def _publish_tiebreak_threads(manager, service, state: ControlState, templates) -> None:
    if not state.tiebreak_required or state.unsupported_tie:
        return
    from modules.community.live_arena import qualification_panel, result_views

    config = service.repository.config
    forum = await qualification_panel._resolve_channel(
        manager.bot, int(config["MATCH_FORUM_CHANNEL_ID"])
    )
    helper = QualificationService(
        service.sheet_id,
        registration_repository=service.registration_repository,
        qualification_repository=service.repository,
        clock=service.clock,
    )

    for match in state.tiebreak_matches:
        if _text(match.get("thread_id")):
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
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )
        thread = getattr(created, "thread", None)
        if thread is None and isinstance(created, tuple):
            thread = created[0]
        if thread is None:
            thread = created
        try:
            await helper.record_thread_id(_text(match["match_id"]), str(thread.id))
            match["thread_id"] = str(thread.id)
            get_partial = getattr(thread, "get_partial_message", None)
            starter = (
                get_partial(int(thread.id))
                if callable(get_partial)
                else await thread.fetch_message(int(thread.id))
            )
            await starter.edit(view=result_views.MatchResultView(str(manager.sheet_id)))
        except Exception:
            try:
                await thread.delete(reason="Live Arena tiebreak thread persistence failed")
            except Exception:
                log.exception("Live Arena untracked tiebreak thread cleanup failed")
            raise


async def _ensure_tiebreak_flow(manager) -> ControlState:
    service = knockout.KnockoutService(manager.sheet_id)
    await service.initialize()
    templates = await _load_templates(manager.sheet_id)
    state = await _ensure_tiebreak_sheet_state(service)
    if state.tiebreak_required and not state.unsupported_tie:
        await _publish_tiebreak_threads(manager, service, state, templates)
        await _materialize_tiebreak_resolutions(service, state)
    manager._qualification_tiebreak_required = (
        state.tiebreak_required and not state.tiebreak_complete
    )
    manager._qualification_tiebreak_state = state
    return state


def _status_icon(row) -> str:
    if row is None:
        return "⬜ Not started"
    status = _text(row.get("status")).lower()
    if status in {"closed", "frozen", "resolved"}:
        return "✅ Finished"
    if status == "ready_to_close":
        return "⚠️ Ready to finish"
    if status in {"active", "open", "published", "published/open", "correction_in_progress"}:
        return "🟦 In progress"
    if status in {"preview", "approved"}:
        return "🟨 Preparing"
    return status.replace("_", " ").title() or "Not started"


def _stage_summary(state: ControlState) -> tuple[str, str, str]:
    tid = state.tournament_id
    q1 = knockout._round_by_id(state.rounds, tid, f"{tid}-Q1")
    q2 = knockout._round_by_id(state.rounds, tid, f"{tid}-Q2")
    q3 = knockout._round_by_id(state.rounds, tid, f"{tid}-Q3")
    seed = knockout._seed_row(state.rounds, tid)
    qf = _round(state.rounds, tid, stage="quarterfinal")
    sf = _round(state.rounds, tid, stage="semifinal")
    final = _round(state.rounds, tid, stage="final")

    for number, row in ((1, q1), (2, q2), (3, q3)):
        if row is not None and _text(row.get("status")) != "closed":
            return (
                f"Qualification Round {number}",
                "Finish the current qualification round.",
                f"Qualification Round {number + 1}" if number < 3 else "Lock the Top 8",
            )
    if q3 is not None and _text(q3.get("status")) == "closed" and state.tiebreak_required and not state.tiebreak_complete:
        return (
            "Qualification finished",
            "Complete the qualification tiebreak matchup.",
            "Lock the Top 8",
        )
    if q3 is not None and _text(q3.get("status")) == "closed" and seed is None:
        return ("Qualification finished", "Lock the Top 8.", "Quarterfinals")
    for label, row, next_label in (
        ("Quarterfinals", qf, "Semifinals"),
        ("Semifinals", sf, "Final"),
        ("Final", final, "Finish the tournament"),
    ):
        if row is not None and _text(row.get("status")) != "closed":
            return (label, f"Complete the {label.lower()}.", next_label)
    if final is not None and _text(final.get("status")) == "closed":
        return ("Final finished", "Finish the tournament.", "Tournament complete")
    if seed is not None:
        return ("Top 8 locked", "Prepare the Quarterfinals.", "Quarterfinals")
    return ("Tournament setup", "Prepare the next tournament step.", "Next tournament step")


def _progress_lines(state: ControlState) -> str:
    tid = state.tournament_id
    rows = [
        ("Qualification Round 1", knockout._round_by_id(state.rounds, tid, f"{tid}-Q1")),
        ("Qualification Round 2", knockout._round_by_id(state.rounds, tid, f"{tid}-Q2")),
        ("Qualification Round 3", knockout._round_by_id(state.rounds, tid, f"{tid}-Q3")),
    ]
    lines = [f"{name} — {_status_icon(row)}" for name, row in rows]
    q3 = rows[-1][1]
    if q3 is not None and _text(q3.get("status")) == "closed":
        if state.tiebreak_required:
            tb_status = "✅ Finished" if state.tiebreak_complete else "⚠️ Waiting"
            lines.append(f"Qualification tiebreak — {tb_status}")
        else:
            lines.append("Qualification tiebreak — ✅ Not required")
    seed = knockout._seed_row(state.rounds, tid)
    lines.append(f"Top 8 — {'✅ Locked' if seed is not None else '🔒 Not locked'}")
    for label, stage in (("Quarterfinals", "quarterfinal"), ("Semifinals", "semifinal"), ("Final", "final")):
        lines.append(f"{label} — {_status_icon(_round(state.rounds, tid, stage=stage))}")
    return "\n".join(lines)


def _resolved_standings(state: ControlState):
    if not state.tiebreak_complete:
        return state.standings
    resolutions = knockout_tiebreak._read_resolutions(
        knockout_tiebreak._tiebreak_row(state.rounds, state.tournament_id)
    )
    affected = knockout._competitive_ties(state.standings[:8], state.standings)
    ordered = knockout_tiebreak._apply_resolutions(state.standings, affected, resolutions)
    return ordered or state.standings


def _standings_lines(state: ControlState) -> str:
    tie_ids = {uid for group in state.tie_groups for uid in group}
    lines = []
    for index, entry in enumerate(_resolved_standings(state)[:8], 1):
        marker = " ⚠️ tied" if entry.discord_user_id in tie_ids and not state.tiebreak_complete else ""
        lines.append(
            f"**#{index} · {entry.match_record}** <@{entry.discord_user_id}>{marker}"
        )
    return "\n".join(lines) or "No finalized qualification results yet."


def _attention_lines(state: ControlState) -> list[str]:
    lines: list[str] = []
    disputed = [
        row for row in state.matches if _text(row.get("status")).lower() == "disputed"
    ]
    if disputed:
        lines.append(f"⚠️ **{len(disputed)} disputed result{'s' if len(disputed) != 1 else ''} need review.**")
    ready = [
        row for row in state.rounds if _text(row.get("status")).lower() == "ready_to_close"
        and _text(row.get("round_stage")).lower() != _TB_STAGE
    ]
    for row in ready:
        lines.append(f"✅ **{_text(row.get('round_name'))}** is ready to finish.")
    if state.unsupported_tie:
        names = ", ".join(
            next((entry.display_name for entry in state.standings if entry.discord_user_id == uid), uid)
            for group in state.tie_groups
            for uid in group
        )
        lines.append(f"⚠️ Qualification order still has a multi-player tie: **{names}**. Organizer review is required.")
    elif state.tiebreak_required and not state.tiebreak_complete:
        for match in state.tiebreak_matches:
            thread = _text(match.get("thread_id"))
            link = f" <#{thread}>" if thread else ""
            lines.append(
                f"⚠️ **Qualification tiebreak:** {_text(match.get('player_a_display_name'))} vs "
                f"{_text(match.get('player_b_display_name'))}.{link}"
            )
    return lines


async def _render_control_center(manager, state: ControlState) -> None:
    config, _ = await load_pr5_config(manager.sheet_id)
    message_id = _text(config.get("ORGANIZER_PANEL_MESSAGE_ID"))
    if not message_id:
        return
    channel = manager.bot.get_channel(int(config["ORGANIZER_CHANNEL_ID"]))
    if channel is None:
        channel = await manager.bot.fetch_channel(int(config["ORGANIZER_CHANNEL_ID"]))
    message = await channel.fetch_message(int(message_id))
    if not getattr(message, "embeds", None):
        return
    templates = await _load_templates(manager.sheet_id)
    embed = discord.Embed.from_dict(message.embeds[0].to_dict())
    embed.clear_fields()

    stage, current_step, next_step = _stage_summary(state)
    title, description = templates["organizer_control_stage"].render(
        stage=stage,
        current_step=current_step,
        next_step=next_step,
    )
    embed.add_field(name=title or "Current tournament state", value=description, inline=False)

    attention = _attention_lines(state)
    if attention:
        title, description = templates["organizer_control_attention"].render(
            attention_lines="\n".join(attention)
        )
        embed.add_field(name=title or "Attention needed", value=description, inline=False)

    title, description = templates["organizer_control_progress"].render(
        progress_lines=_progress_lines(state)
    )
    embed.add_field(name=title or "Tournament progress", value=description, inline=False)

    if state.standings:
        title, description = templates["organizer_control_standings"].render(
            standings_lines=_standings_lines(state)
        )
        embed.add_field(name=title or "Current qualification order", value=description, inline=False)

    _, tournament, _, _, _ = await manager.data(getattr(channel, "guild", None))
    await message.edit(embed=embed, view=manager.view(tournament.status))


async def _open_tiebreak_callback(self, interaction: discord.Interaction):
    if not await OrganizerView(self.manager).authorized(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        state = await _ensure_tiebreak_flow(self.manager)
        templates = await _load_templates(self.manager.sheet_id)
        if not state.tiebreak_required:
            await interaction.followup.send(
                embed=templates["qualification_tiebreak_not_required"].embed(),
                ephemeral=True,
            )
            return
        if state.unsupported_tie:
            raise RegistrationError(
                "More than two players are tied in the same qualification position. This needs organizer review before the Top 8 can be locked."
            )
        players = []
        links = []
        for match in state.tiebreak_matches:
            players.append(
                f"{_text(match.get('player_a_display_name'))} vs {_text(match.get('player_b_display_name'))}"
            )
            thread_id = _text(match.get("thread_id"))
            links.append(f"• <#{thread_id}>" if thread_id else "• Match thread is being created")
        status = "Finished" if state.tiebreak_complete else "Waiting for the matchup result"
        await interaction.followup.send(
            embed=templates["qualification_tiebreak_open"].embed(
                players="; ".join(players),
                state=status,
                thread_links="\n".join(links),
            ),
            ephemeral=True,
        )
        await _render_control_center(self.manager, state)
    except Exception as exc:
        log.exception("Live Arena qualification tiebreak control failed")
        await interaction.followup.send(embed=error_embed(exc), ephemeral=True)


async def _freeze_with_playable_tiebreaks(original, service, actor_id: str):
    state = await _ensure_tiebreak_sheet_state(service)
    if state.tiebreak_required:
        await _materialize_tiebreak_resolutions(service, state)
        rounds = await service.repository.rounds()
        resolutions = knockout_tiebreak._read_resolutions(
            knockout_tiebreak._tiebreak_row(rounds, state.tournament_id)
        )
        affected = knockout._competitive_ties(state.standings[:8], state.standings)
        if knockout_tiebreak._apply_resolutions(state.standings, affected, resolutions) is None:
            names = ", ".join(
                entry.display_name
                for entry in state.standings
                if entry.discord_user_id in {uid for group in state.tie_groups for uid in group}
            )
            raise RegistrationError(
                "Qualification tiebreak required. "
                f"{names} still need to complete their extra matchup before the Top 8 can be locked. "
                "Open the qualification tiebreak from Captain's Table for the match thread."
            )
    return await original(service, actor_id)


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import qualification_panel

    original_freeze = knockout.KnockoutService.freeze_top8

    async def freeze_top8(self, actor_id: str):
        return await _freeze_with_playable_tiebreaks(original_freeze, self, actor_id)

    knockout.KnockoutService.freeze_top8 = freeze_top8

    # Replace the internal-ID entry modal with a direct route to the known match.
    original_button_init = knockout_tiebreak.RecordTiebreakButton.__init__

    def tiebreak_button_init(self, manager, *, disabled=False):
        original_button_init(self, manager, disabled=disabled)
        self.label = "Open Tiebreak Match"
        self.style = discord.ButtonStyle.primary
        self.disabled = bool(disabled or not getattr(manager, "_qualification_tiebreak_required", False))

    knockout_tiebreak.RecordTiebreakButton.__init__ = tiebreak_button_init
    knockout_tiebreak.RecordTiebreakButton.callback = _open_tiebreak_callback

    original_install = qualification_panel.install_qualification

    def install_with_control_center(manager) -> bool:
        installed = original_install(manager)
        if not installed:
            return False
        if getattr(manager, "_captains_table_control_center_installed", False):
            return True
        manager._captains_table_control_center_installed = True
        manager._qualification_tiebreak_required = False
        base_sync = manager.sync

        async def sync_with_control_center(*args, **kwargs):
            result = await base_sync(*args, **kwargs)
            try:
                state = await _ensure_tiebreak_flow(manager)
                await _render_control_center(manager, state)
            except Exception:
                log.exception("Live Arena Captain's Table control-center refresh failed")
            return result

        manager.sync = sync_with_control_center
        return True

    qualification_panel.install_qualification = install_with_control_center
