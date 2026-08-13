"""Use fixed-length Live Arena sets and apply the final tournament UX layer."""

from __future__ import annotations

from shared.sheets.async_core import sheet_read_scope
from modules.community.live_arena.registration import RegistrationError
from modules.community.live_arena.service import _text

_installed = False

_FRIENDLY_LABELS = {
    "Open Registration": "Open Signups",
    "Close Registration": "Close Signups",
    "Reopen Registration": "Reopen Signups",
    "View Roster": "View Players",
    "Reconcile Roles": "Fix Player Roles",
    "Complete Tournament": "Finish Tournament",
    "Archive Tournament": "Archive Tournament",
    "Generate Q1 Draw": "Create Round 1 Matchups",
    "Approve Draw": "Publish Round 1",
    "Regenerate Draw": "Redo Matchups",
    "Swap Players": "Swap Opponents",
    "Close Current Round": "Finish Round",
    "Review Result Issues": "Review Match Issues",
    "Reopen Closed Round": "Reopen Round",
    "Repair Discord State": "Repair Tournament",
    "View Standings": "View Standings",
    "Preview Next Swiss": "Preview Next Round",
    "Regenerate Swiss Preview": "Redo Next Round",
    "Approve & Publish Swiss": "Publish Next Round",
    "Repair Swiss Conflict": "Fix Matchup Conflict",
    "Freeze Top 8": "Lock Top 8",
    "Approve & Open Knockout": "Start Knockout Stage",
    "Record BO3 Tiebreak": "Record Tiebreak",
    "Competition Ops": "Organizer Actions",
    "Create Next Tournament": "Create New Tournament",
    "Player History": "Player History",
}

_FRIENDLY_ROWS = {
    "Open Signups": 0,
    "Close Signups": 0,
    "Reopen Signups": 0,
    "Create Round 1 Matchups": 0,
    "Publish Round 1": 0,
    "Redo Matchups": 0,
    "Swap Opponents": 0,
    "Finish Round": 0,
    "View Standings": 0,
    "Preview Next Round": 0,
    "Redo Next Round": 0,
    "Publish Next Round": 0,
    "Lock Top 8": 0,
    "Start Knockout Stage": 0,
    "Finish Tournament": 0,
    "Archive Tournament": 0,
    "Create New Tournament": 0,
    "Review Match Issues": 1,
    "Organizer Actions": 1,
    "Fix Matchup Conflict": 1,
    "Record Tiebreak": 1,
    "Reopen Round": 1,
    "View Players": 2,
    "Fix Player Roles": 2,
    "Player History": 2,
    "Repair Tournament": 3,
}


def _validate_full_set_score(round_row, score_a: int, score_b: int) -> None:
    try:
        a, b = int(score_a), int(score_b)
    except (TypeError, ValueError) as exc:
        raise RegistrationError("Result scores must be whole numbers") from exc

    is_final = _text(round_row.get("round_stage")).lower() == "final"
    fights = 5 if is_final else 3
    if a < 0 or b < 0 or a + b != fights or a == b:
        if is_final:
            raise RegistrationError(
                "Final result must contain all 5 fights: 5-0, 4-1, 3-2, 2-3, 1-4, or 0-5"
            )
        raise RegistrationError(
            "BO3 result must contain all 3 fights: 3-0, 2-1, 1-2, or 0-3"
        )


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import (
        competition,
        competition_resolution,
        qualification_panel,
        result_views,
        simulation_ux_finalizer,
        simulation_ux_hardening,
    )

    competition._validate_played_score = _validate_full_set_score
    competition_resolution._validate_played_score = _validate_full_set_score

    original_match_embed = qualification_panel.match_embed

    def match_embed_with_full_set_wording(tournament, round_row, match, slots):
        embed = original_match_embed(tournament, round_row, match, slots)
        final = _text(round_row.get("round_stage")).lower() == "final"
        description = str(embed.description or "")
        if final:
            description = description.replace(
                "**Format:** Best of 5",
                "**Format:** Best of 5 · 5 fights · play all 5",
            )
            description = description.replace(
                "**Format:** Best of 3",
                "**Format:** Best of 5 · 5 fights · play all 5",
            )
        else:
            description = description.replace(
                "**Format:** Best of 3",
                "**Format:** Best of 3 · 3 fights · play all 3",
            )
        embed.description = description
        return embed

    qualification_panel.match_embed = match_embed_with_full_set_wording

    try:
        result_views.ReportResultModal.score.placeholder = "3-0 or 2-1 · Final: 5-0, 4-1, 3-2"
    except Exception:
        pass
    try:
        simulation_ux_hardening.HardenedReportResultModal.score.placeholder = (
            "3-0 or 2-1 · Final: 5-0, 4-1, 3-2"
        )
    except Exception:
        pass

    _install_captains_table_ux(qualification_panel, simulation_ux_finalizer)
    _install_final_ledger_cleanup(qualification_panel, simulation_ux_hardening)


def _install_captains_table_ux(qualification_panel, simulation_ux_finalizer) -> None:
    original_install = qualification_panel.install_qualification

    def install_mobile_first(manager) -> bool:
        installed = original_install(manager)
        if not installed:
            return False
        if getattr(manager, "_captains_table_final_ux_installed", False):
            return True
        manager._captains_table_final_ux_installed = True
        base_view = manager.view
        base_sync = manager.sync
        manager._captains_table_allowed = None

        def view(status=None):
            result = base_view(status)
            # status=None is persistent callback registration. Keep those callbacks
            # available, but keep the actual visible message state-first and small.
            if status is None:
                return result
            allowed = getattr(manager, "_captains_table_allowed", None)
            if not allowed:
                return result
            for item in list(result.children):
                original_label = _text(getattr(item, "label", ""))
                if original_label and original_label not in allowed:
                    result.remove_item(item)
                    continue
                friendly = _FRIENDLY_LABELS.get(original_label)
                if friendly:
                    item.label = friendly
                    item.row = _FRIENDLY_ROWS.get(friendly)
            return result

        async def sync():
            try:
                with sheet_read_scope():
                    manager._captains_table_allowed = (
                        await simulation_ux_finalizer._allowed_panel_actions(manager)
                    )
            except Exception:
                manager._captains_table_allowed = None
            return await base_sync()

        manager.view = view
        manager.sync = sync
        return True

    qualification_panel.install_qualification = install_mobile_first


def _install_final_ledger_cleanup(qualification_panel, simulation_ux_hardening) -> None:
    """Keep the friendly thread jump link after every real guild publisher refresh."""
    original_reconcile = qualification_panel.QualificationPublisher.reconcile

    async def reconcile_with_friendly_links(publisher):
        warnings = list(await original_reconcile(publisher))
        try:
            config = publisher.service.repository.config
            channel = publisher.bot.get_channel(int(config["ROUND_OVERVIEW_CHANNEL_ID"]))
            if channel is None:
                channel = await publisher.bot.fetch_channel(int(config["ROUND_OVERVIEW_CHANNEL_ID"]))
            if getattr(channel, "guild", None) is None:
                return list(dict.fromkeys(warnings))
            snapshot = await publisher.service.snapshot()
            if snapshot.round_row is not None:
                await simulation_ux_hardening._refresh_victory_ledger_links(
                    publisher.bot, publisher.service, snapshot
                )
        except Exception:
            warnings.append("Victory Ledger match links")
        return list(dict.fromkeys(warnings))

    qualification_panel.QualificationPublisher.reconcile = reconcile_with_friendly_links
