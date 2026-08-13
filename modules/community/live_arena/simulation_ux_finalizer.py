"""Final refinements for the simulation UX hardening layer."""

from __future__ import annotations

import discord

from shared.theme import colors

from modules.community.live_arena.service import _text, load_tournament_snapshot

_installed = False


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import simulation_ux_hardening as ux

    ux._submit_report = _submit_report
    ux._allowed_panel_actions = _allowed_panel_actions


async def _submit_report(
    interaction,
    service,
    match,
    *,
    reporter_id: str,
    submitted_by_id: str,
    raw_score: str,
) -> None:
    """Submit using Sheet-side scores but present the score reporter-first."""
    from modules.community.live_arena import result_views, simulation_ux_hardening as ux

    score_a, score_b = result_views._score_for_sheet_sides(raw_score, reporter_id, match)
    updated = await service.report_result(
        reporter_id,
        _text(match["match_id"]),
        score_a,
        score_b,
        screenshot_present=True,
    )
    if submitted_by_id != reporter_id:
        await ux._audit_organizer_submission(
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
    score_text = _normalize_score(raw_score)
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
            f"{ux._discord_timestamp(due)}. No second confirmation is required."
        )
        dispute_text = (
            f"<@{opponent_id}> may dispute this result until {ux._discord_timestamp(due)}. "
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
    await ux._post_thread_notice(interaction.channel, notice, title=title)
    await result_views._run_post_mutation_sync(service.sheet_id)


def _normalize_score(raw: str) -> str:
    return str(raw or "").strip().replace("–", "-").replace("—", "-")


async def _allowed_panel_actions(manager) -> set[str]:
    """Return only actions that make sense in the current tournament lifecycle."""
    from modules.community.live_arena.qualification import QualificationService

    tournament = await load_tournament_snapshot(manager.sheet_id)
    status = _text(tournament.status)
    maintenance = {"Repair Discord State", "Player History"}
    roster = {"View Roster", "Reconcile Roles"}

    if status == "draft":
        return {"Open Registration"} | roster | maintenance
    if status == "signup_open":
        return {"Close Registration"} | roster | maintenance
    if status == "completed":
        return {"Archive Tournament", "Create Next Tournament", "View Standings"} | maintenance
    if status == "archived":
        return {"Create Next Tournament"} | maintenance

    service = QualificationService(manager.sheet_id)
    await service.initialize()
    tid = service.repository.config["ACTIVE_TOURNAMENT_ID"]
    rounds = [
        row
        for row in await service.repository.rounds()
        if _text(row.get("tournament_id")) == tid
    ]

    if status == "signup_closed":
        q1 = _qualification_round(rounds, 1)
        if q1 is None:
            return {"Reopen Registration", "Generate Q1 Draw"} | roster | maintenance
        if _text(q1.get("status")) in {"proposed", "preview"}:
            return {
                "Reopen Registration",
                "Approve Draw",
                "Regenerate Draw",
                "Swap Players",
            } | roster | maintenance
        return {"Reopen Registration"} | roster | maintenance

    if status != "active":
        return roster | maintenance

    open_rounds = [
        row
        for row in rounds
        if _text(row.get("status"))
        in {
            "active",
            "published",
            "open",
            "published/open",
            "ready_to_close",
            "correction_in_progress",
        }
    ]
    if open_rounds:
        current = max(open_rounds, key=_round_sort_key)
        actions = {
            "Close Current Round",
            "Review Result Issues",
            "Competition Ops",
        } | roster | maintenance
        if _text(current.get("round_stage")).lower() == "qualification":
            actions.add("View Standings")
        return actions

    previews = [
        row
        for row in rounds
        if _text(row.get("status")) in {"preview", "approved", "proposed"}
    ]
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
        return {
            "Approve & Open Knockout",
            "Record BO3 Tiebreak",
            "View Standings",
        } | roster | maintenance

    # Knockout completion outranks the permanently closed Q3 row.
    final = _stage_round(rounds, "final")
    if final is not None and _text(final.get("status")) == "closed":
        return {"Complete Tournament", "View Standings"} | roster | maintenance

    knockout_exists = any(
        _text(row.get("round_stage")).lower()
        in {"quarterfinal", "semifinal", "final"}
        for row in rounds
    )
    if knockout_exists:
        # Normally reconciliation creates the next knockout preview immediately.
        # If not, expose repair rather than incorrectly returning to Top 8 freeze.
        return {"View Standings", "Reopen Closed Round"} | roster | maintenance

    closed_q = {
        int(_text(row.get("round_number")) or 0)
        for row in rounds
        if _text(row.get("round_stage")).lower() == "qualification"
        and _text(row.get("status")) == "closed"
    }
    if 3 in closed_q:
        return {
            "View Standings",
            "Freeze Top 8",
            "Record BO3 Tiebreak",
            "Reopen Closed Round",
        } | roster | maintenance
    if closed_q & {1, 2}:
        return {
            "View Standings",
            "Preview Next Swiss",
            "Regenerate Swiss Preview",
            "Approve & Publish Swiss",
            "Repair Swiss Conflict",
            "Reopen Closed Round",
        } | roster | maintenance
    return roster | maintenance


def _qualification_round(rounds, number: int):
    return next(
        (
            row
            for row in rounds
            if _text(row.get("round_stage")).lower() == "qualification"
            and int(_text(row.get("round_number")) or 0) == number
        ),
        None,
    )


def _stage_round(rounds, stage: str):
    return next(
        (row for row in rounds if _text(row.get("round_stage")).lower() == stage),
        None,
    )


def _round_sort_key(row):
    stage = _text(row.get("round_stage")).lower()
    order = {"qualification": 1, "quarterfinal": 2, "semifinal": 3, "final": 4}
    return (order.get(stage, 0), int(_text(row.get("round_number")) or 0))
