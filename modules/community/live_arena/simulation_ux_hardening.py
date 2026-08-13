"""Simulation-found Live Arena UX hardening.

This installer runs last so it can tighten the fully decorated runtime without
changing the Google Sheets contract.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from uuid import uuid4

import discord

from shared.sheets.async_core import sheet_read_scope
from shared.theme import colors

from modules.community.live_arena.registration import RegistrationError
from modules.community.live_arena.service import _text, load_tournament_snapshot
from modules.community.live_arena.views import error_embed

log = logging.getLogger("c1c.community.live_arena.simulation_ux_hardening")
_installed = False

_OPEN_ROUND_STATUSES = {
    "active",
    "published",
    "open",
    "published/open",
    "ready_to_close",
    "correction_in_progress",
}
_PREVIEW_STATUSES = {"preview", "approved", "proposed"}


def install() -> None:
    """Install the final result, naming, ledger, and organizer-panel fixes."""
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import qualification_panel, result_views, runtime_hooks

    _install_result_reporting(result_views)
    _install_round_discord_cleanup(runtime_hooks, qualification_panel)
    _install_lifecycle_panel(qualification_panel)


def _install_result_reporting(result_views) -> None:
    original_init = result_views.MatchResultView.__init__

    def init_with_hardened_report(
        self,
        sheet_id: str,
        *,
        report_disabled: bool = False,
        dispute_disabled: bool = False,
    ):
        original_init(
            self,
            sheet_id,
            report_disabled=report_disabled,
            dispute_disabled=dispute_disabled,
        )
        for item in list(self.children):
            if getattr(item, "custom_id", "") == "live_arena:match:report_result":
                self.remove_item(item)
                break
        self.add_item(HardenedReportResultButton(sheet_id, disabled=report_disabled))

    result_views.MatchResultView.__init__ = init_with_hardened_report
    # competition_followup replaced this helper with an older two-argument
    # function. Restore one compatible helper for both result and dispute notices.
    result_views._post_thread_notice = _post_thread_notice
    # Evidence is the presence of an image in the matchup record. It does not need
    # to have been uploaded by one particular participant, which also supports an
    # organizer helping a player who cannot use the reporting UI themselves.
    result_views._thread_has_result_screenshot = _thread_has_result_screenshot


class HardenedReportResultButton(discord.ui.Button):
    def __init__(self, sheet_id: str, *, disabled: bool = False):
        super().__init__(
            label="Report Result",
            style=discord.ButtonStyle.success,
            custom_id="live_arena:match:report_result",
            disabled=disabled,
        )
        self.sheet_id = str(sheet_id)

    async def callback(self, interaction: discord.Interaction):
        # No Sheet read before the initial Discord acknowledgement. The modal is
        # opened immediately, so a quota spike can never become "didn't respond".
        await interaction.response.send_modal(HardenedReportResultModal(self.sheet_id))


class HardenedReportResultModal(discord.ui.Modal, title="Report Match Result"):
    score = discord.ui.TextInput(
        label="Final series score",
        placeholder="2-1 (BO3) or 3-2 (Final BO5)",
        min_length=3,
        max_length=5,
    )

    def __init__(self, sheet_id: str):
        super().__init__(timeout=300)
        self.sheet_id = str(sheet_id)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            from modules.community.live_arena.competition_resolution import CompetitionResolutionService
            from modules.community.live_arena.messages import load_pr5_config

            with sheet_read_scope():
                service = CompetitionResolutionService(self.sheet_id)
                await service.initialize()
                match = await service.match_for_thread(str(interaction.channel_id))
                actor = str(interaction.user.id)
                players = {
                    _text(match["player_a_discord_user_id"]),
                    _text(match["player_b_discord_user_id"]),
                }
                if not await _thread_has_result_screenshot(interaction.channel, players):
                    await interaction.followup.send(
                        embed=discord.Embed(
                            title="Screenshot required",
                            description=(
                                "Post at least one screenshot of the match result in this "
                                "Duelling Deck thread, then use **Report Result** again."
                            ),
                            color=colors.c1c_blue,
                        ),
                        ephemeral=True,
                    )
                    return

                if actor in players:
                    await _submit_report(
                        interaction,
                        service,
                        match,
                        reporter_id=actor,
                        submitted_by_id=actor,
                        raw_score=str(self.score.value),
                    )
                    return

                config, _ = await load_pr5_config(self.sheet_id)
                organizer_role = _text(config["ORGANIZER_ROLE_ID"])
                authorized = any(
                    str(getattr(role, "id", "")) == organizer_role
                    for role in getattr(interaction.user, "roles", ())
                )
                if not authorized:
                    raise RegistrationError(
                        "Only the two matchup participants or a configured tournament organizer can report this result."
                    )

                await interaction.followup.send(
                    embed=discord.Embed(
                        title="Report on behalf of a participant",
                        description=(
                            "Choose which participant this score is being submitted for. "
                            "The thread and audit log will record that the organizer submitted it on their behalf."
                        ),
                        color=colors.c1c_blue,
                    ),
                    view=OrganizerReporterChoice(
                        self.sheet_id,
                        _text(match["match_id"]),
                        str(self.score.value),
                        match,
                    ),
                    ephemeral=True,
                )
        except Exception as exc:
            log.exception("Live Arena hardened result-report preflight failed")
            await interaction.followup.send(embed=error_embed(exc), ephemeral=True)


class OrganizerReporterChoice(discord.ui.View):
    def __init__(self, sheet_id: str, match_id: str, raw_score: str, match):
        super().__init__(timeout=300)
        self.add_item(
            OrganizerReporterSelect(sheet_id, match_id, raw_score, match)
        )


class OrganizerReporterSelect(discord.ui.Select):
    def __init__(self, sheet_id: str, match_id: str, raw_score: str, match):
        self.sheet_id = str(sheet_id)
        self.match_id = str(match_id)
        self.raw_score = str(raw_score)
        options = []
        for side in ("a", "b"):
            uid = _text(match[f"player_{side}_discord_user_id"])
            label = _text(match.get(f"player_{side}_display_name")) or uid
            options.append(discord.SelectOption(label=label[:100], value=uid))
        super().__init__(
            placeholder="Choose the participant you are reporting for",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            from modules.community.live_arena.competition_resolution import CompetitionResolutionService
            from modules.community.live_arena.messages import load_pr5_config

            with sheet_read_scope():
                config, _ = await load_pr5_config(self.sheet_id)
                organizer_role = _text(config["ORGANIZER_ROLE_ID"])
                if not any(
                    str(getattr(role, "id", "")) == organizer_role
                    for role in getattr(interaction.user, "roles", ())
                ):
                    raise RegistrationError("You no longer have the configured organizer role")

                service = CompetitionResolutionService(self.sheet_id)
                await service.initialize()
                match = await service.match_for_thread(str(interaction.channel_id))
                if _text(match["match_id"]) != self.match_id:
                    raise RegistrationError("This match thread no longer matches the report form")
                if not await _thread_has_result_screenshot(interaction.channel, set()):
                    raise RegistrationError(
                        "Post at least one result screenshot in this Duelling Deck thread before reporting the score."
                    )
                await _submit_report(
                    interaction,
                    service,
                    match,
                    reporter_id=str(self.values[0]),
                    submitted_by_id=str(interaction.user.id),
                    raw_score=self.raw_score,
                )
        except Exception as exc:
            log.exception("Live Arena organizer on-behalf result report failed")
            await interaction.followup.send(embed=error_embed(exc), ephemeral=True)


async def _submit_report(
    interaction,
    service,
    match,
    *,
    reporter_id: str,
    submitted_by_id: str,
    raw_score: str,
) -> None:
    from modules.community.live_arena import result_views

    score_a, score_b = result_views._score_for_sheet_sides(raw_score, reporter_id, match)
    updated = await service.report_result(
        reporter_id,
        _text(match["match_id"]),
        score_a,
        score_b,
        screenshot_present=True,
    )
    if submitted_by_id != reporter_id:
        await _audit_organizer_submission(
            service,
            updated,
            organizer_id=submitted_by_id,
            participant_id=reporter_id,
            score_a=score_a,
            score_b=score_b,
        )

    status = _text(updated.get("status"))
    due = _text(updated.get("confirm_due_at_utc"))
    if status == "pending_confirmation" and due:
        result_views.schedule_match_finalization(
            service.sheet_id,
            _text(updated["match_id"]),
            due,
        )

    opponent_id = (
        _text(updated["player_b_discord_user_id"])
        if reporter_id == _text(updated["player_a_discord_user_id"])
        else _text(updated["player_a_discord_user_id"])
    )
    score_text = f"{_text(updated['reported_score_a'])}-{_text(updated['reported_score_b'])}"
    if status == "organizer_review":
        followup = (
            f"Recorded **{score_text}**. The Final is awaiting explicit organizer confirmation."
        )
        dispute_text = "Organizer confirmation is required before the Final becomes official."
    elif status == "late_review":
        followup = f"Recorded **{score_text}**. This late result is awaiting organizer review."
        dispute_text = "This late report is waiting for organizer review."
    else:
        followup = (
            f"Recorded **{score_text}**. <@{opponent_id}> may dispute it until "
            f"{_discord_timestamp(due)}. No second confirmation is required."
        )
        dispute_text = (
            f"<@{opponent_id}> may dispute this result until {_discord_timestamp(due)}. "
            "If no dispute is raised, the result finalizes automatically; no second confirmation is required."
        )

    await interaction.followup.send(
        embed=discord.Embed(
            title="Result reported",
            description=followup,
            color=colors.c1c_blue,
        ),
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
    )

    if submitted_by_id == reporter_id:
        notice = (
            f"<@{reporter_id}> reported **{score_text}** against <@{opponent_id}>.\n"
            f"{dispute_text}"
        )
        title = "Result reported"
    else:
        notice = (
            f"<@{submitted_by_id}> reported **{score_text}** on behalf of "
            f"<@{reporter_id}> against <@{opponent_id}>.\n{dispute_text}"
        )
        title = "Result reported by organizer"
    await _post_thread_notice(interaction.channel, notice, title=title)
    await result_views._run_post_mutation_sync(service.sheet_id)


async def _audit_organizer_submission(
    service,
    match,
    *,
    organizer_id: str,
    participant_id: str,
    score_a: int,
    score_b: int,
) -> None:
    try:
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        await service.registration_repository.append_audit(
            dict(
                event_id=str(uuid4()),
                tournament_id=_text(match["tournament_id"]),
                event_type="match_result_reported_on_behalf",
                actor_discord_user_id=str(organizer_id),
                target_discord_user_id=str(participant_id),
                details=json.dumps(
                    {
                        "match_id": _text(match["match_id"]),
                        "reported_on_behalf_of": str(participant_id),
                        "submitted_by_organizer": str(organizer_id),
                        "score_a": int(score_a),
                        "score_b": int(score_b),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                created_at_utc=now,
            )
        )
    except Exception:
        log.exception(
            "Live Arena organizer on-behalf provenance audit failed • match=%s",
            _text(match.get("match_id")),
        )


async def _thread_has_result_screenshot(channel, _player_ids: set[str]) -> bool:
    """Any image in the persistent matchup thread satisfies the evidence gate."""
    history = getattr(channel, "history", None)
    if not callable(history):
        return False
    async for message in history(limit=None):
        for attachment in getattr(message, "attachments", ()):
            content_type = str(getattr(attachment, "content_type", "") or "").lower()
            filename = str(getattr(attachment, "filename", "") or "").lower()
            if content_type.startswith("image/") or filename.endswith(
                (".png", ".jpg", ".jpeg", ".webp")
            ):
                return True
    return False


async def _post_thread_notice(channel, content: str, *, title: str) -> None:
    try:
        await channel.send(
            embed=discord.Embed(
                title=title,
                description=str(content),
                color=colors.c1c_blue,
            ),
            allowed_mentions=discord.AllowedMentions(
                users=True,
                roles=False,
                everyone=False,
            ),
        )
    except Exception:
        log.exception("Live Arena matchup thread result/dispute notice failed")


def _discord_timestamp(value: str) -> str:
    if not value:
        return "the organizer review"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return "the objection deadline"
    return f"<t:{int(parsed.timestamp())}:F>"


def _install_round_discord_cleanup(runtime_hooks, qualification_panel) -> None:
    original_sync = runtime_hooks._sync_round_discord

    async def sync_with_clean_resources(bot, qualification_service, snapshot):
        warnings = list(await original_sync(bot, qualification_service, snapshot))
        try:
            warnings.extend(
                await _rename_match_threads(bot, qualification_service, snapshot)
            )
        except Exception:
            log.exception("Live Arena tournament-scoped thread rename failed")
            warnings.append("match thread names")
        try:
            await _refresh_victory_ledger_links(bot, qualification_service, snapshot)
        except Exception:
            log.exception("Live Arena Victory Ledger link cleanup failed")
            warnings.append("Victory Ledger match links")
        return list(dict.fromkeys(warnings))

    runtime_hooks._sync_round_discord = sync_with_clean_resources


async def _rename_match_threads(bot, service, snapshot) -> list[str]:
    if snapshot.round_row is None:
        return []
    _, (_, tournament), _, _ = await service.context()
    warnings = []
    for match in snapshot.matches:
        thread_id = _text(match.get("thread_id"))
        if not thread_id:
            continue
        try:
            thread = bot.get_channel(int(thread_id))
            if thread is None:
                thread = await bot.fetch_channel(int(thread_id))
            desired = _match_thread_name(tournament, snapshot.round_row, match)
            if _text(getattr(thread, "name", "")) != desired:
                await thread.edit(name=desired, reason="Live Arena tournament-scoped naming")
        except Exception:
            log.exception(
                "Live Arena match thread rename failed • match=%s",
                _text(match.get("match_id")),
            )
            warnings.append(f"Match {_text(match.get('match_number'))} thread name")
    return warnings


def _tournament_resource_label(tournament) -> str:
    short_name = _text(tournament.get("tournament_short_name")) or _text(
        tournament.get("tournament_name")
    )
    source = (
        _text(tournament.get("signup_opens_at_utc"))
        or _text(tournament.get("created_at_utc"))
    )
    prefix = ""
    if source:
        try:
            prefix = datetime.fromisoformat(source.replace("Z", "+00:00")).strftime("%y-%m")
        except ValueError:
            prefix = ""
    return " ".join(part for part in (prefix, short_name) if part).strip()


def _round_code(round_row) -> str:
    stage = _text(round_row.get("round_stage")).lower()
    if stage == "qualification":
        return f"Q{int(_text(round_row.get('round_number')) or 0)}"
    return {
        "quarterfinal": "QF",
        "semifinal": "SF",
        "final": "Final",
    }.get(stage, _text(round_row.get("round_name")) or "Round")


def _match_thread_name(tournament, round_row, match) -> str:
    prefix = (
        f"{_tournament_resource_label(tournament)} • {_round_code(round_row)} • "
        f"M{int(_text(match.get('match_number')) or 0):02d} • "
    )
    player_a = _text(match.get("player_a_display_name")) or _text(
        match.get("player_a_discord_user_id")
    )
    player_b = _text(match.get("player_b_display_name")) or _text(
        match.get("player_b_discord_user_id")
    )
    available = max(12, 100 - len(prefix) - len(" vs "))
    each = max(5, available // 2)
    a = _trim_name(player_a, each)
    b = _trim_name(player_b, available - len(a))
    return f"{prefix}{a} vs {b}"[:100]


def _trim_name(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    if limit <= 1:
        return value[:limit]
    return value[: limit - 1] + "…"


async def _refresh_victory_ledger_links(bot, service, snapshot) -> None:
    if snapshot.round_row is None:
        return
    overview_id = _text(snapshot.round_row.get("overview_message_id"))
    if not overview_id:
        return
    config = service.repository.config
    from modules.community.live_arena import qualification_panel, runtime_hooks

    channel = await qualification_panel._resolve_channel(
        bot, int(config["ROUND_OVERVIEW_CHANNEL_ID"])
    )
    message = await channel.fetch_message(int(overview_id))
    _, (_, tournament), _, _ = await service.context()
    standings = []
    if _text(snapshot.round_row.get("round_stage")).lower() == "qualification":
        from modules.community.live_arena.competition_resolution import CompetitionResolutionService

        competition = CompetitionResolutionService(service.sheet_id)
        await competition.initialize()
        standings = await competition.standings()
    embed = runtime_hooks._competition_overview_embed(
        tournament,
        snapshot.round_row,
        [dict(row) for row in snapshot.matches],
        standings,
    )
    guild_id = _text(getattr(getattr(channel, "guild", None), "id", ""))
    sorted_matches = sorted(
        snapshot.matches,
        key=lambda row: int(_text(row.get("match_number")) or 0),
    )
    for index, match in enumerate(sorted_matches):
        thread_id = _text(match.get("thread_id"))
        link = "Forum post pending"
        if thread_id and guild_id:
            link = (
                "💬 [Open match thread]"
                f"(https://discord.com/channels/{guild_id}/{thread_id})"
            )
        result = runtime_hooks._public_match_result(match)
        embed.set_field_at(
            index,
            name=f"Match {_text(match.get('match_number'))}",
            value=(
                f"<@{_text(match.get('player_a_discord_user_id'))}> vs "
                f"<@{_text(match.get('player_b_discord_user_id'))}>\n"
                f"{result}\n{link}"
            ),
            inline=False,
        )
    await message.edit(embed=embed)


def _install_lifecycle_panel(qualification_panel) -> None:
    original_install = qualification_panel.install_qualification

    def install_with_lifecycle_panel(manager) -> bool:
        installed = original_install(manager)
        if not installed:
            return False
        if getattr(manager, "_simulation_lifecycle_panel_installed", False):
            return True
        manager._simulation_lifecycle_panel_installed = True
        base_view = manager.view
        base_sync = manager.sync
        manager._lifecycle_allowed_labels = None

        def view(status=None):
            result = base_view(status)
            allowed = getattr(manager, "_lifecycle_allowed_labels", None)
            if status is None or not allowed:
                return result
            for item in list(result.children):
                label = _text(getattr(item, "label", ""))
                if label and label not in allowed:
                    result.remove_item(item)
            return result

        async def sync():
            try:
                with sheet_read_scope():
                    manager._lifecycle_allowed_labels = await _allowed_panel_actions(manager)
            except Exception:
                log.exception("Live Arena organizer lifecycle-state refresh failed")
            return await base_sync()

        manager.view = view
        manager.sync = sync
        return True

    qualification_panel.install_qualification = install_with_lifecycle_panel


async def _allowed_panel_actions(manager) -> set[str]:
    from modules.community.live_arena.qualification import QualificationService

    tournament = await load_tournament_snapshot(manager.sheet_id)
    status = _text(tournament.status)
    maintenance = {"Repair Discord State", "Player History"}
    roster = {"View Roster", "Reconcile Roles"}

    if status == "draft":
        return {"Open Registration"} | roster | maintenance
    if status == "signup_open":
        return {"Close Registration"} | roster | maintenance

    service = QualificationService(manager.sheet_id)
    await service.initialize()
    config = service.repository.config
    rounds = [
        row
        for row in await service.repository.rounds()
        if _text(row.get("tournament_id")) == config["ACTIVE_TOURNAMENT_ID"]
    ]

    if status == "signup_closed":
        q1 = _round_by_number(rounds, 1)
        if q1 is None:
            return {"Reopen Registration", "Generate Q1 Draw"} | roster | maintenance
        q1_status = _text(q1.get("status"))
        if q1_status in {"proposed", "preview"}:
            return {
                "Reopen Registration",
                "Approve Draw",
                "Regenerate Draw",
                "Swap Players",
            } | roster | maintenance
        return {"Reopen Registration"} | roster | maintenance

    if status == "completed":
        return {
            "Archive Tournament",
            "Create Next Tournament",
            "View Standings",
        } | maintenance
    if status == "archived":
        return {"Create Next Tournament"} | maintenance
    if status != "active":
        return maintenance

    active_rounds = [
        row for row in rounds if _text(row.get("status")) in _OPEN_ROUND_STATUSES
    ]
    if active_rounds:
        current = max(active_rounds, key=_round_sort_key)
        actions = {
            "Close Current Round",
            "Review Result Issues",
            "Competition Ops",
        } | roster | maintenance
        if _text(current.get("round_stage")).lower() == "qualification":
            actions.add("View Standings")
        return actions

    previews = [row for row in rounds if _text(row.get("status")) in _PREVIEW_STATUSES]
    if previews:
        current = max(previews, key=_round_sort_key)
        stage = _text(current.get("round_stage")).lower()
        if stage == "qualification":
            return {
                "View Standings",
                "Preview Next Swiss",
                "Regenerate Swiss Preview",
                "Approve & Publish Swiss",
                "Repair Swiss Conflict",
                "Reopen Closed Round",
            } | roster | maintenance
        if stage in {"quarterfinal", "semifinal", "final"}:
            return {
                "Approve & Open Knockout",
                "Record BO3 Tiebreak",
                "View Standings",
            } | roster | maintenance

    q_rounds = [
        row
        for row in rounds
        if _text(row.get("round_stage")).lower() == "qualification"
        and _text(row.get("status")) == "closed"
    ]
    q_numbers = {
        int(_text(row.get("round_number")) or 0)
        for row in q_rounds
    }
    if 3 in q_numbers:
        return {
            "View Standings",
            "Freeze Top 8",
            "Record BO3 Tiebreak",
            "Reopen Closed Round",
        } | roster | maintenance
    if q_numbers & {1, 2}:
        return {
            "View Standings",
            "Preview Next Swiss",
            "Regenerate Swiss Preview",
            "Approve & Publish Swiss",
            "Repair Swiss Conflict",
            "Reopen Closed Round",
        } | roster | maintenance

    final = next(
        (
            row
            for row in rounds
            if _text(row.get("round_stage")).lower() == "final"
            and _text(row.get("status")) == "closed"
        ),
        None,
    )
    if final is not None:
        return {"Complete Tournament", "View Standings"} | roster | maintenance

    return roster | maintenance


def _round_by_number(rounds, number: int):
    for row in rounds:
        if (
            _text(row.get("round_stage")).lower() == "qualification"
            and int(_text(row.get("round_number")) or 0) == number
        ):
            return row
    return None


def _round_sort_key(row):
    stage = _text(row.get("round_stage")).lower()
    stage_order = {
        "qualification": 1,
        "quarterfinal": 2,
        "semifinal": 3,
        "final": 4,
    }.get(stage, 0)
    return (stage_order, int(_text(row.get("round_number")) or 0))
