"""Final refinements for the simulation UX hardening layer."""

from __future__ import annotations

from modules.community.live_arena.service import _text, load_tournament_snapshot

_installed = False


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import simulation_ux_hardening as ux

    original_submit = ux._submit_report

    async def submit_with_reporter_score(
        interaction,
        service,
        match,
        *,
        reporter_id: str,
        submitted_by_id: str,
        raw_score: str,
    ):
        # The score entered in the modal is always reporter-first. Keep that wording
        # for the public notice even when the reporter is Sheet-side player B.
        original_post = ux._post_thread_notice
        original_send = interaction.followup.send
        reporter_score = _normalize_score(raw_score)

        async def post_with_score(channel, content: str, *, title: str):
            content = _replace_sheet_score_with_reporter_score(
                content,
                match,
                reporter_id,
                reporter_score,
            )
            await original_post(channel, content, title=title)

        async def send_with_score(*args, **kwargs):
            embed = kwargs.get("embed")
            if embed is not None and getattr(embed, "description", None):
                embed.description = _replace_sheet_score_with_reporter_score(
                    embed.description,
                    match,
                    reporter_id,
                    reporter_score,
                )
            return await original_send(*args, **kwargs)

        ux._post_thread_notice = post_with_score
        interaction.followup.send = send_with_score
        try:
            return await original_submit(
                interaction,
                service,
                match,
                reporter_id=reporter_id,
                submitted_by_id=submitted_by_id,
                raw_score=raw_score,
            )
        finally:
            interaction.followup.send = original_send
            ux._post_thread_notice = original_post

    # Keep the implementation above intentionally local to one submission. The
    # lifecycle correction below is the lasting runtime patch.
    ux._submit_report = submit_with_reporter_score
    ux._allowed_panel_actions = _allowed_panel_actions


def _normalize_score(raw: str) -> str:
    return str(raw or "").strip().replace("–", "-").replace("—", "-")


def _replace_sheet_score_with_reporter_score(
    text: str,
    match,
    reporter_id: str,
    reporter_score: str,
) -> str:
    if reporter_id == _text(match.get("player_a_discord_user_id")):
        return text
    parts = reporter_score.split("-")
    if len(parts) != 2:
        return text
    sheet_score = f"{parts[1]}-{parts[0]}"
    return str(text).replace(f"**{sheet_score}**", f"**{reporter_score}**")


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
        row for row in rounds if _text(row.get("status")) in {
            "active", "published", "open", "published/open",
            "ready_to_close", "correction_in_progress",
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
        row for row in rounds
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

    # Knockout completion must outrank the permanently closed Q3 row.
    final = _stage_round(rounds, "final")
    if final is not None and _text(final.get("status")) == "closed":
        return {"Complete Tournament", "View Standings"} | roster | maintenance

    knockout_exists = any(
        _text(row.get("round_stage")).lower()
        in {"quarterfinal", "semifinal", "final"}
        for row in rounds
    )
    if knockout_exists:
        # Normally the reconciliation layer creates the next knockout preview
        # immediately. If it has not appeared yet, expose repair instead of
        # incorrectly returning to the Top 8 freeze controls.
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
            row for row in rounds
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
