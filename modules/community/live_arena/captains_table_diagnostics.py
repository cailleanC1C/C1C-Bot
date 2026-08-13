"""Temporary diagnostics for the real Captain's Table render path."""

from __future__ import annotations

import logging

from shared.sheets.async_core import sheet_read_scope
from modules.community.live_arena.messages import load_pr5_config
from modules.community.live_arena.repository import LiveArenaRepository
from modules.community.live_arena.service import _text, load_tournament_snapshot

log = logging.getLogger("c1c.community.live_arena.captains_table_diagnostics")
_installed = False
_PREFIX = "LIVE_ARENA_CAPTAINS_TABLE_DIAG"


def _view_labels(view) -> list[str]:
    return [str(getattr(item, "label", "") or "") for item in getattr(view, "children", ()) if str(getattr(item, "label", "") or "")]


async def _diagnostic_context(manager):
    tournament = await load_tournament_snapshot(manager.sheet_id)
    config, _ = await load_pr5_config(manager.sheet_id)
    repository = LiveArenaRepository(manager.sheet_id)
    await repository.initialize()
    resource = await repository.discord_resource(tournament.tournament_id, "organizer_panel", "main")
    registry_message_id = _text(resource["message_id"]) if resource else ""
    legacy_message_id = _text(config.get("ORGANIZER_PANEL_MESSAGE_ID", ""))
    return tournament, registry_message_id or legacy_message_id, bool(registry_message_id)


async def _diagnostic_allowed_actions(manager):
    from modules.community.live_arena import simulation_ux_finalizer
    return sorted(await simulation_ux_finalizer._allowed_panel_actions(manager))


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import tournament_lifecycle
    original_sync = tournament_lifecycle._sync_organizer_panel

    async def diagnostic_sync(manager):
        tournament = None
        message_id = "unknown"
        message_source = "unknown"
        allowed_actions = None
        try:
            with sheet_read_scope():
                tournament, message_id, from_registry = await _diagnostic_context(manager)
                message_source = "resource_registry" if from_registry else "legacy_config"
                try:
                    allowed_actions = await _diagnostic_allowed_actions(manager)
                except Exception as exc:
                    log.exception("%s lifecycle-action calculation FAILED • manager_id=%s • tournament_id=%s • status=%s • error=%s: %s", _PREFIX, id(manager), _text(getattr(tournament, "tournament_id", "")), _text(getattr(tournament, "status", "")), type(exc).__name__, exc)
        except Exception as exc:
            log.exception("%s context lookup FAILED • manager_id=%s • error=%s: %s", _PREFIX, id(manager), type(exc).__name__, exc)

        original_view = manager.view
        render_calls = 0

        def traced_view(status=None):
            nonlocal render_calls
            render_calls += 1
            view = original_view(status)
            labels = _view_labels(view)
            log.warning("%s final-view-build • manager_id=%s • call=%s • tournament_id=%s • status_arg=%s • target_message_id=%s • target_source=%s • allowed_actions=%s • component_count=%s • labels=%s", _PREFIX, id(manager), render_calls, _text(getattr(tournament, "tournament_id", "")), _text(status), message_id, message_source, allowed_actions if allowed_actions is not None else "UNAVAILABLE", len(labels), labels)
            return view

        manager.view = traced_view
        try:
            result = await original_sync(manager)
            log.warning("%s sync-complete • manager_id=%s • tournament_id=%s • target_message_id=%s • render_calls=%s • ok=%s • operation=%s", _PREFIX, id(manager), _text(getattr(tournament, "tournament_id", "")), message_id, render_calls, getattr(result, "ok", None), getattr(result, "operation", ""))
            return result
        except Exception as exc:
            log.exception("%s sync-FAILED • manager_id=%s • tournament_id=%s • target_message_id=%s • render_calls=%s • error=%s: %s", _PREFIX, id(manager), _text(getattr(tournament, "tournament_id", "")), message_id, render_calls, type(exc).__name__, exc)
            raise
        finally:
            manager.view = original_view

    tournament_lifecycle._sync_organizer_panel = diagnostic_sync
