"""Final Victory Ledger refresh boundary.

The canonical round renderer is Sheet-backed and may be decorated by later Live Arena
UX layers. This installer runs last and guarantees that every competition sync ends
with one authoritative Victory Ledger write from current Sheet truth. It also owns
the visible last-updated timestamp.

This is deliberately a final boundary rather than another result-state patch: report,
confirm, dispute, organizer resolution, timeout finalization, startup reconciliation,
and repair all converge on the same round overview refresh.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import discord

from shared.config import cfg
from shared.sheets.async_core import afetch_values, sheet_read_scope

from modules.community.live_arena.messages import MESSAGE_HEADERS, load_pr5_config
from modules.community.live_arena.service import LiveArenaConfigError, _enabled, _rows, _text

log = logging.getLogger("c1c.community.live_arena.victory_ledger_final_refresh")
_installed = False
_last_updated_labels: dict[str, str] = {}


async def _load_last_updated_label(sheet_id: str) -> str:
    """Load the Sheet-owned footer label without extending older message contracts."""
    sid = str(sheet_id or "").strip()
    if not sid:
        raise LiveArenaConfigError("Victory Ledger last-updated copy requires a Sheet ID")

    config, _ = await load_pr5_config(sid)
    tab = config["MESSAGES_TAB"]
    rows = _rows(await afetch_values(sid, tab) or [], MESSAGE_HEADERS, tab)
    matches = [
        row
        for row in rows
        if _text(row.get("message_key")) == "round_overview_last_updated"
        and _enabled(row.get("active"))
    ]
    if len(matches) != 1:
        raise LiveArenaConfigError(
            "MESSAGES: required active row missing or duplicated: round_overview_last_updated"
        )

    label = _text(matches[0].get("title"))
    if not label:
        raise LiveArenaConfigError(
            "MESSAGES.round_overview_last_updated: title must not be blank"
        )
    if "{" in label or "}" in label:
        raise LiveArenaConfigError(
            "MESSAGES.round_overview_last_updated: title must not contain placeholders"
        )

    _last_updated_labels[sid] = label
    return label


def _stamp_last_updated(
    embeds: list[discord.Embed],
    sheet_id: str,
    *,
    now: datetime | None = None,
) -> list[discord.Embed]:
    """Put one visible timestamp on the final embed of the overview message."""
    if not embeds:
        return embeds
    label = _last_updated_labels.get(str(sheet_id))
    if not label:
        return embeds

    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    else:
        moment = moment.astimezone(UTC)

    embeds[-1].set_footer(text=label)
    embeds[-1].timestamp = moment
    return embeds


async def _force_overview_refresh(bot, qualification_service, snapshot) -> bool:
    """Rewrite the current Victory Ledger overview from authoritative current state."""
    round_row = getattr(snapshot, "round_row", None)
    if round_row is None:
        return False

    from modules.community.live_arena import qualification_panel, round_overview
    from modules.community.live_arena.competition_resolution import (
        CompetitionResolutionService,
    )

    config = qualification_service.repository.config
    overview_channel = await qualification_panel._resolve_channel(
        bot, int(config["ROUND_OVERVIEW_CHANNEL_ID"])
    )
    guild_id = _text(
        getattr(getattr(overview_channel, "guild", None), "id", "")
    )

    _, (_, tournament), _, _ = await qualification_service.context()
    standings = []
    if _text(round_row.get("round_stage")).lower() == "qualification":
        competition_service = CompetitionResolutionService(
            qualification_service.sheet_id
        )
        await competition_service.initialize()
        standings = await competition_service.standings()

    embeds = await round_overview.render_round_overview_embeds(
        sheet_id=qualification_service.sheet_id,
        tournament=tournament,
        round_row=round_row,
        matches=[dict(row) for row in snapshot.matches],
        standings=standings,
        guild_id=guild_id,
    )

    overview_id = _text(round_row.get("overview_message_id"))
    message = None
    if overview_id:
        try:
            message = await overview_channel.fetch_message(int(overview_id))
        except discord.NotFound:
            message = None

    if message is not None:
        await message.edit(embeds=embeds)
    else:
        created = await overview_channel.send(embeds=embeds)
        try:
            await qualification_service.record_overview_message_id(
                _text(round_row.get("round_id")), str(created.id)
            )
        except Exception:
            try:
                await created.delete()
            except Exception:
                log.exception(
                    "Live Arena untracked final Victory Ledger overview cleanup failed"
                )
            raise

    terminal = {"finalized", "forfeit", "double_forfeit", "bye"}
    completed = sum(
        _text(row.get("status")) in terminal for row in snapshot.matches
    )
    log.info(
        "Live Arena final Victory Ledger refresh succeeded • round=%s • status=%s • completed=%s/%s • message=%s",
        _text(round_row.get("round_id")),
        _text(round_row.get("status")),
        completed,
        len(snapshot.matches),
        overview_id or "created",
    )
    return True


def install() -> None:
    """Install after all other Live Arena result/render decorators."""
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import panel, round_overview, runtime_hooks

    # Preload the user-facing footer copy in the normal startup Sheet read scope.
    original_register = panel.register_live_arena

    async def register_with_last_updated_copy(bot):
        sid = str(cfg.get("LIVE_ARENA_TOURNAMENT_SHEET_ID", "") or "").strip()
        with sheet_read_scope():
            if sid:
                await _load_last_updated_label(sid)
            return await original_register(bot)

    panel.register_live_arena = register_with_last_updated_copy

    # Stamp every canonical render, including startup, normal mutation refreshes,
    # closed-round snapshots, and explicit repair/migration renders.
    original_render = round_overview.render_round_overview_embeds

    async def render_with_last_updated(**kwargs):
        embeds = await original_render(**kwargs)
        return _stamp_last_updated(embeds, str(kwargs.get("sheet_id") or ""))

    round_overview.render_round_overview_embeds = render_with_last_updated

    # Older migration logic may consider an existing three-embed message healthy
    # even if the real content refresh just failed. Finish every sync with one
    # authoritative write so stale-but-correct-shaped panels cannot survive.
    original_sync = runtime_hooks._sync_round_discord

    async def sync_with_final_victory_ledger(bot, qualification_service, snapshot):
        warnings = list(await original_sync(bot, qualification_service, snapshot))
        try:
            refreshed = await _force_overview_refresh(
                bot, qualification_service, snapshot
            )
            if refreshed:
                warnings = [
                    item for item in warnings if item != "Victory Ledger overview"
                ]
        except Exception as exc:
            log.exception(
                "Live Arena final Victory Ledger refresh failed • round=%s • error=%s: %s",
                _text(
                    getattr(snapshot, "round_row", {}).get("round_id")
                    if getattr(snapshot, "round_row", None)
                    else ""
                ),
                type(exc).__name__,
                exc,
            )
            warnings.append("Victory Ledger overview")
        return list(dict.fromkeys(warnings))

    runtime_hooks._sync_round_discord = sync_with_final_victory_ledger
