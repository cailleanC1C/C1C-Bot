"""Final Victory Ledger refresh boundary.

The canonical round renderer is Sheet-backed and may be decorated by later Live Arena
UX layers. This installer runs last and guarantees that every competition sync ends
with one authoritative Victory Ledger write from current Sheet truth. It also owns
the visible last-updated timestamp and the Captain's Table round-closure alert.

This is deliberately a final boundary rather than another result-state patch: report,
confirm, dispute, organizer resolution, timeout finalization, startup reconciliation,
and repair all converge on the same round overview refresh.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from string import Formatter

import discord

from shared.config import cfg
from shared.sheets.async_core import afetch_values, sheet_read_scope

from modules.community.live_arena.messages import MESSAGE_HEADERS, load_pr5_config
from modules.community.live_arena.service import LiveArenaConfigError, _enabled, _rows, _text

log = logging.getLogger("c1c.community.live_arena.victory_ledger_final_refresh")
_installed = False
_last_updated_labels: dict[str, str] = {}
_round_alert_copy: dict[str, dict[str, "_AlertTemplate"]] = {}
_ROUND_READY_MARKER_PREFIX = "live_arena:round_ready:"
_ROUND_ALERT_CONTRACTS = {
    "round_ready_to_close_alert": {"round_name", "completed", "total_matches"},
    "round_ready_to_close_closed": {"round_name"},
}
_TERMINAL_MATCH_STATUSES = {"finalized", "forfeit", "double_forfeit", "bye"}


@dataclass(frozen=True)
class _AlertTemplate:
    key: str
    title: str
    description: str
    color: int

    def embed(self, **values: object) -> discord.Embed:
        expected = _ROUND_ALERT_CONTRACTS[self.key]
        fields = {
            name
            for _, name, _, _ in Formatter().parse(self.title + self.description)
            if name
        }
        if fields != expected:
            raise LiveArenaConfigError(
                f"MESSAGES.{self.key}: placeholders must be exactly "
                + ", ".join(sorted(expected))
            )
        missing = fields - values.keys()
        if missing:
            raise LiveArenaConfigError(
                f"MESSAGES.{self.key}: missing render value {', '.join(sorted(missing))}"
            )
        return discord.Embed(
            title=self.title.format(**values),
            description=self.description.format(**values),
            color=self.color,
        )


def _parse_color(key: str, value: object) -> int:
    color_text = _text(value)
    if len(color_text) != 7 or not color_text.startswith("#"):
        raise LiveArenaConfigError(f"MESSAGES.{key}: color_hex must be #RRGGBB")
    try:
        return int(color_text[1:], 16)
    except ValueError as exc:
        raise LiveArenaConfigError(
            f"MESSAGES.{key}: color_hex must be #RRGGBB"
        ) from exc


async def _message_rows(sheet_id: str) -> list[dict[str, object]]:
    config, _ = await load_pr5_config(sheet_id)
    tab = config["MESSAGES_TAB"]
    return _rows(await afetch_values(sheet_id, tab) or [], MESSAGE_HEADERS, tab)


async def _load_last_updated_label(sheet_id: str) -> str:
    """Load the Sheet-owned footer label without extending older message contracts."""
    sid = str(sheet_id or "").strip()
    if not sid:
        raise LiveArenaConfigError("Victory Ledger last-updated copy requires a Sheet ID")

    rows = await _message_rows(sid)
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


async def _load_round_alert_copy(sheet_id: str) -> dict[str, _AlertTemplate]:
    """Load Sheet-owned Captain's Table copy for the ready-to-close lifecycle."""
    sid = str(sheet_id or "").strip()
    if not sid:
        raise LiveArenaConfigError("Round closure alert copy requires a Sheet ID")

    rows = await _message_rows(sid)
    templates: dict[str, _AlertTemplate] = {}
    for key, expected in _ROUND_ALERT_CONTRACTS.items():
        matches = [
            row
            for row in rows
            if _text(row.get("message_key")) == key and _enabled(row.get("active"))
        ]
        if len(matches) != 1:
            raise LiveArenaConfigError(
                f"MESSAGES: required active row missing or duplicated: {key}"
            )
        row = matches[0]
        template = _AlertTemplate(
            key=key,
            title=_text(row.get("title")),
            description=_text(row.get("description")),
            color=_parse_color(key, row.get("color_hex")),
        )
        fields = {
            name
            for _, name, _, _ in Formatter().parse(template.title + template.description)
            if name
        }
        if fields != expected:
            raise LiveArenaConfigError(
                f"MESSAGES.{key}: placeholders must be exactly "
                + ", ".join(sorted(expected))
            )
        template.embed(**{name: "x" for name in expected})
        templates[key] = template

    _round_alert_copy[sid] = templates
    return templates


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


def _normalize_standings_spacing(embeds: list[discord.Embed]) -> list[discord.Embed]:
    """Keep the cumulative context on its own paragraph above the first standing."""
    prefix = "*Includes finalized results through Round "
    for embed in embeds:
        description = str(embed.description or "")
        if not description.startswith(prefix):
            continue
        context_end = description.find(".*")
        if context_end < 0:
            continue
        split_at = context_end + 2
        context = description[:split_at].rstrip()
        standings = description[split_at:].lstrip()
        embed.description = (
            f"{context}\n\n{standings}" if standings else context
        )[:4096]
        break
    return embeds


async def _organizer_channel(bot, sheet_id: str):
    config, _ = await load_pr5_config(sheet_id)
    channel_id = int(config["ORGANIZER_CHANNEL_ID"])
    channel = bot.get_channel(channel_id)
    if channel is None:
        channel = await bot.fetch_channel(channel_id)
    return channel, _text(config["ORGANIZER_ROLE_ID"])


async def _find_round_ready_alert(channel, bot, round_id: str):
    marker = f"{_ROUND_READY_MARKER_PREFIX}{round_id}"
    bot_id = _text(getattr(getattr(bot, "user", None), "id", ""))
    history = getattr(channel, "history", None)
    if not callable(history):
        return None
    async for message in history(limit=100):
        author_id = _text(getattr(getattr(message, "author", None), "id", ""))
        if bot_id and author_id and author_id != bot_id:
            continue
        for embed in getattr(message, "embeds", ()):
            footer = _text(getattr(getattr(embed, "footer", None), "text", ""))
            if footer == marker:
                return message
    return None


async def _sync_round_ready_alert(bot, sheet_id: str, round_row, matches) -> None:
    """Ping Captain's Table exactly once when a round becomes ready to close."""
    status = _text(round_row.get("status")).lower()
    if status not in {"ready_to_close", "closed"}:
        return

    templates = _round_alert_copy.get(str(sheet_id))
    if templates is None:
        templates = await _load_round_alert_copy(str(sheet_id))

    channel, organizer_role_id = await _organizer_channel(bot, str(sheet_id))
    round_id = _text(round_row.get("round_id"))
    round_name = _text(round_row.get("round_name")) or round_id
    existing = await _find_round_ready_alert(channel, bot, round_id)
    marker = f"{_ROUND_READY_MARKER_PREFIX}{round_id}"

    if status == "ready_to_close":
        completed = sum(
            _text(row.get("status")).lower() in _TERMINAL_MATCH_STATUSES
            for row in matches
        )
        embed = templates["round_ready_to_close_alert"].embed(
            round_name=round_name,
            completed=completed,
            total_matches=len(matches),
        )
        embed.set_footer(text=marker)
        if existing is not None:
            # Reconciliation/redeploy may revisit this state. Update the existing
            # alert but never ping organizers a second time for the same round.
            await existing.edit(embed=embed)
            return
        await channel.send(
            content=f"<@&{organizer_role_id}>",
            embed=embed,
            allowed_mentions=discord.AllowedMentions(
                users=False, roles=True, everyone=False
            ),
        )
        log.info(
            "Live Arena round-ready Captain's Table alert sent • round=%s",
            round_id,
        )
        return

    if existing is None:
        return
    embed = templates["round_ready_to_close_closed"].embed(round_name=round_name)
    embed.set_footer(text=marker)
    await existing.edit(content="", embed=embed)
    log.info(
        "Live Arena round-ready Captain's Table alert resolved • round=%s",
        round_id,
    )


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

    completed = sum(
        _text(row.get("status")).lower() in _TERMINAL_MATCH_STATUSES
        for row in snapshot.matches
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

    # Preload all user-facing copy in the normal startup Sheet read scope.
    original_register = panel.register_live_arena

    async def register_with_last_updated_copy(bot):
        sid = str(cfg.get("LIVE_ARENA_TOURNAMENT_SHEET_ID", "") or "").strip()
        with sheet_read_scope():
            if sid:
                await _load_last_updated_label(sid)
                await _load_round_alert_copy(sid)
            return await original_register(bot)

    panel.register_live_arena = register_with_last_updated_copy

    # Stamp every canonical render, including startup, normal mutation refreshes,
    # closed-round snapshots, and explicit repair/migration renders. The final
    # boundary also normalizes the live standings context spacing so Sheet copy
    # cannot run directly into the first ranking line.
    original_render = round_overview.render_round_overview_embeds

    async def render_with_last_updated(**kwargs):
        embeds = await original_render(**kwargs)
        embeds = _normalize_standings_spacing(embeds)
        return _stamp_last_updated(embeds, str(kwargs.get("sheet_id") or ""))

    round_overview.render_round_overview_embeds = render_with_last_updated

    # Older migration logic may consider an existing three-embed message healthy
    # even if the real content refresh just failed. Existing persisted overviews
    # therefore get one final authoritative rewrite after every sync. A brand-new
    # round with no overview ID is left to the normal sync path unless that path
    # reported a Victory Ledger failure, preventing duplicate first-publish posts.
    original_sync = runtime_hooks._sync_round_discord

    async def sync_with_final_victory_ledger(bot, qualification_service, snapshot):
        warnings = list(await original_sync(bot, qualification_service, snapshot))
        round_row = getattr(snapshot, "round_row", None)
        overview_id = _text(round_row.get("overview_message_id")) if round_row else ""
        needs_final_refresh = bool(overview_id) or "Victory Ledger overview" in warnings

        if needs_final_refresh:
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
                    _text(round_row.get("round_id")) if round_row else "",
                    type(exc).__name__,
                    exc,
                )
                warnings.append("Victory Ledger overview")

        if round_row is not None:
            try:
                await _sync_round_ready_alert(
                    bot,
                    qualification_service.sheet_id,
                    round_row,
                    [dict(row) for row in snapshot.matches],
                )
            except Exception as exc:
                log.exception(
                    "Live Arena Captain's Table round-ready alert sync failed • round=%s • error=%s: %s",
                    _text(round_row.get("round_id")),
                    type(exc).__name__,
                    exc,
                )
                warnings.append("Captain's Table round closure alert")

        return list(dict.fromkeys(warnings))

    runtime_hooks._sync_round_discord = sync_with_final_victory_ledger
