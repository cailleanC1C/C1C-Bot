"""Mobile-first Victory Ledger navigation, archive, results, and Hall of Fame routing.

The parent Victory Ledger channel is intentionally kept small: one persistent index
plus at most one currently active round overview. Closed-round snapshots are copied
once into an immutable Round Archive thread, final tournament recaps live in a
Tournament Results thread, and the cross-tournament Hall of Fame lives in its own
thread.

All visible copy and thread names are Sheet-owned through MESSAGES. Discord resource
IDs are persisted in TOURNAMENT_DISCORD_RESOURCES; no thread IDs are hard-coded.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from string import Formatter

import discord

from shared.sheets.async_core import afetch_values

from modules.community.live_arena.competition import calculate_qualification_standings
from modules.community.live_arena.competition_resolution import CompetitionResolutionService
from modules.community.live_arena.messages import MESSAGE_HEADERS
from modules.community.live_arena.repository import LiveArenaRepository
from modules.community.live_arena.service import (
    TOURNAMENT_HEADERS,
    LiveArenaConfigError,
    _enabled,
    _rows,
    _text,
)

log = logging.getLogger("c1c.community.live_arena.victory_ledger_workspace")

_GLOBAL_RESOURCE_ID = "__LIVE_ARENA_GLOBAL__"
_THREAD_RESOURCE_TYPE = "victory_ledger_thread"
_INDEX_RESOURCE_TYPE = "victory_ledger_index"
_CURRENT_RESOURCE_TYPE = "victory_ledger_current"
_ARCHIVE_RESOURCE_TYPE = "round_archive"

_PUBLIC_ROUND_STATUSES = {
    "active",
    "published",
    "open",
    "published/open",
    "ready_to_close",
    "correction_in_progress",
}

_COPY_CONTRACTS = {
    "victory_ledger_index": {
        "current_round",
        "round_archive",
        "tournament_results",
        "hall_of_fame",
    },
    "victory_ledger_current_link": {"url"},
    "victory_ledger_no_active_round": set(),
    "victory_ledger_round_archive_thread": set(),
    "victory_ledger_tournament_results_thread": set(),
    "victory_ledger_hall_of_fame_thread": set(),
}

_TEMPLATE_CACHE: dict[str, dict[str, "Template"]] = {}
_reconcile_tasks: dict[str, asyncio.Task] = {}
_installed = False
_original_final_recap = None


@dataclass(frozen=True)
class Template:
    key: str
    title: str
    description: str
    color: int

    def render(self, **values: object) -> tuple[str, str]:
        expected = _COPY_CONTRACTS[self.key]
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
        return self.title.format(**values), self.description.format(**values)

    def embed(self, **values: object) -> discord.Embed:
        title, description = self.render(**values)
        return discord.Embed(title=title, description=description, color=self.color)


@dataclass
class Workspace:
    repository: LiveArenaRepository
    parent: object
    archive: object
    results: object
    hall_of_fame: object
    templates: dict[str, Template]


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


async def _templates(sheet_id: str, messages_tab: str) -> dict[str, Template]:
    sid = str(sheet_id)
    cached = _TEMPLATE_CACHE.get(sid)
    if cached is not None:
        return cached
    rows = _rows(
        await afetch_values(sid, messages_tab) or [],
        MESSAGE_HEADERS,
        messages_tab,
    )
    result: dict[str, Template] = {}
    for key, expected in _COPY_CONTRACTS.items():
        matches = [
            row
            for row in rows
            if _text(row["message_key"]) == key and _enabled(row["active"])
        ]
        if len(matches) != 1:
            raise LiveArenaConfigError(
                f"MESSAGES: required active row missing or duplicated: {key}"
            )
        row = matches[0]
        color_text = _text(row["color_hex"])
        if len(color_text) != 7 or not color_text.startswith("#"):
            raise LiveArenaConfigError(f"MESSAGES.{key}: color_hex must be #RRGGBB")
        template = Template(
            key,
            _text(row["title"]),
            _text(row["description"]),
            int(color_text[1:], 16),
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
        result[key] = template
    _TEMPLATE_CACHE[sid] = result
    return result


def clear_template_cache(sheet_id: str | None = None) -> None:
    if sheet_id is None:
        _TEMPLATE_CACHE.clear()
    else:
        _TEMPLATE_CACHE.pop(str(sheet_id), None)


async def _resolve_channel(bot, channel_id: str):
    if not channel_id:
        return None
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        channel = await bot.fetch_channel(int(channel_id))
    return channel


async def _fetch_message(channel, message_id: str):
    if channel is None or not message_id:
        return None
    try:
        return await channel.fetch_message(int(message_id))
    except discord.NotFound:
        return None


async def _delete_message(channel, message_id: str) -> None:
    message = await _fetch_message(channel, message_id)
    if message is None:
        return
    try:
        await message.delete()
    except discord.NotFound:
        return


def _channel_link(guild_id: str, channel_id: str, message_id: str = "") -> str:
    base = f"https://discord.com/channels/{guild_id}/{channel_id}"
    return f"{base}/{message_id}" if message_id else base


def _thread_link(template: Template, guild_id: str, thread_id: str) -> str:
    return f"[{template.title}]({_channel_link(guild_id, thread_id)})"


async def _ensure_thread(
    *,
    bot,
    repository: LiveArenaRepository,
    parent,
    key: str,
    template: Template,
):
    resource = await repository.discord_resource(
        _GLOBAL_RESOURCE_ID, _THREAD_RESOURCE_TYPE, key
    )
    thread_id = _text(resource.get("thread_id")) if resource else ""
    thread = None
    if thread_id:
        try:
            thread = await _resolve_channel(bot, thread_id)
        except discord.NotFound:
            thread = None
    if thread is None:
        for candidate in getattr(parent, "threads", ()):
            if _text(getattr(candidate, "name", "")) == template.title:
                thread = candidate
                break
    if thread is None:
        thread = await parent.create_thread(
            name=template.title[:100],
            type=discord.ChannelType.public_thread,
            auto_archive_duration=10080,
            reason="C1C Live Arena Victory Ledger workspace",
        )
    elif getattr(thread, "archived", False):
        await thread.edit(archived=False, reason="Live Arena workspace refresh")

    intro = None
    if resource and _text(resource.get("message_id")):
        intro = await _fetch_message(thread, _text(resource["message_id"]))
    if intro is None:
        intro = await thread.send(embed=template.embed())
    else:
        await intro.edit(embed=template.embed())

    now = _now()
    await repository.upsert_discord_resource(
        tournament_id=_GLOBAL_RESOURCE_ID,
        resource_type=_THREAD_RESOURCE_TYPE,
        resource_key=key,
        channel_id=str(parent.id),
        message_id=str(intro.id),
        thread_id=str(thread.id),
        created_at_utc=_text(resource.get("created_at_utc")) if resource else now,
        updated_at_utc=now,
        state="active",
        notes=f"Victory Ledger workspace thread: {key}",
    )
    return thread


async def ensure_workspace(
    bot,
    sheet_id: str,
    repository: LiveArenaRepository | None = None,
) -> Workspace:
    repository = repository or LiveArenaRepository(str(sheet_id))
    if not repository.config:
        await repository.initialize()
    parent = await _resolve_channel(bot, _text(repository.config["ROUND_OVERVIEW_CHANNEL_ID"]))
    templates = await _templates(str(sheet_id), _text(repository.config["MESSAGES_TAB"]))
    archive = await _ensure_thread(
        bot=bot,
        repository=repository,
        parent=parent,
        key="round_archive",
        template=templates["victory_ledger_round_archive_thread"],
    )
    results = await _ensure_thread(
        bot=bot,
        repository=repository,
        parent=parent,
        key="tournament_results",
        template=templates["victory_ledger_tournament_results_thread"],
    )
    hall = await _ensure_thread(
        bot=bot,
        repository=repository,
        parent=parent,
        key="hall_of_fame",
        template=templates["victory_ledger_hall_of_fame_thread"],
    )
    return Workspace(repository, parent, archive, results, hall, templates)


async def _current_message(workspace: Workspace):
    resource = await workspace.repository.discord_resource(
        _GLOBAL_RESOURCE_ID, _CURRENT_RESOURCE_TYPE, "main"
    )
    if not resource or _text(resource.get("state")) != "active":
        return None
    if _text(resource.get("channel_id")) != str(workspace.parent.id):
        return None
    return await _fetch_message(workspace.parent, _text(resource.get("message_id")))


async def refresh_index(workspace: Workspace, *, current_message=None) -> None:
    guild_id = _text(getattr(getattr(workspace.parent, "guild", None), "id", ""))
    current = current_message if current_message is not None else await _current_message(workspace)
    if current is None:
        current_text = workspace.templates["victory_ledger_no_active_round"].title
    else:
        title, url = workspace.templates["victory_ledger_current_link"].render(
            url=_channel_link(guild_id, str(workspace.parent.id), str(current.id))
        )
        current_text = f"[{title}]({url})"

    embed = workspace.templates["victory_ledger_index"].embed(
        current_round=current_text,
        round_archive=_thread_link(
            workspace.templates["victory_ledger_round_archive_thread"],
            guild_id,
            str(workspace.archive.id),
        ),
        tournament_results=_thread_link(
            workspace.templates["victory_ledger_tournament_results_thread"],
            guild_id,
            str(workspace.results.id),
        ),
        hall_of_fame=_thread_link(
            workspace.templates["victory_ledger_hall_of_fame_thread"],
            guild_id,
            str(workspace.hall_of_fame.id),
        ),
    )
    resource = await workspace.repository.discord_resource(
        _GLOBAL_RESOURCE_ID, _INDEX_RESOURCE_TYPE, "main"
    )
    message = None
    if resource and _text(resource.get("channel_id")) == str(workspace.parent.id):
        message = await _fetch_message(workspace.parent, _text(resource.get("message_id")))
    if message is None:
        message = await workspace.parent.send(embed=embed)
    else:
        await message.edit(embed=embed)
    now = _now()
    await workspace.repository.upsert_discord_resource(
        tournament_id=_GLOBAL_RESOURCE_ID,
        resource_type=_INDEX_RESOURCE_TYPE,
        resource_key="main",
        channel_id=str(workspace.parent.id),
        message_id=str(message.id),
        created_at_utc=_text(resource.get("created_at_utc")) if resource else now,
        updated_at_utc=now,
        state="active",
        notes="Persistent Victory Ledger navigation index",
    )


async def _set_current_resource(
    workspace: Workspace,
    *,
    message_id: str,
    round_id: str,
    state: str,
) -> None:
    resource = await workspace.repository.discord_resource(
        _GLOBAL_RESOURCE_ID, _CURRENT_RESOURCE_TYPE, "main"
    )
    now = _now()
    await workspace.repository.upsert_discord_resource(
        tournament_id=_GLOBAL_RESOURCE_ID,
        resource_type=_CURRENT_RESOURCE_TYPE,
        resource_key="main",
        channel_id=str(workspace.parent.id),
        message_id=message_id,
        created_at_utc=_text(resource.get("created_at_utc")) if resource else now,
        updated_at_utc=now,
        state=state,
        notes=round_id,
    )


async def _archive_round_message(
    workspace: Workspace,
    *,
    tournament_id: str,
    round_id: str,
    embeds,
):
    resource = await workspace.repository.discord_resource(
        tournament_id, _ARCHIVE_RESOURCE_TYPE, round_id
    )
    message = None
    if resource and _text(resource.get("thread_id")) == str(workspace.archive.id):
        message = await _fetch_message(workspace.archive, _text(resource.get("message_id")))
    if message is not None:
        return message

    # Closed-round snapshots are append-only. If the archived message still exists,
    # later repair/reconciliation passes never edit it; organizer commentary beneath
    # it is therefore safe from bot rewrites.
    message = await workspace.archive.send(embeds=list(embeds))
    now = _now()
    await workspace.repository.upsert_discord_resource(
        tournament_id=tournament_id,
        resource_type=_ARCHIVE_RESOURCE_TYPE,
        resource_key=round_id,
        channel_id=str(workspace.archive.id),
        message_id=str(message.id),
        thread_id=str(workspace.archive.id),
        created_at_utc=_text(resource.get("created_at_utc")) if resource else now,
        updated_at_utc=now,
        state=_text(resource.get("state")) or "active" if resource else "active",
        notes="Immutable finalized round snapshot",
    )
    return message


async def _route_round_overview(qualification_service, snapshot, embeds):
    repository = qualification_service.registration_repository
    workspace = await ensure_workspace(
        qualification_service.bot if hasattr(qualification_service, "bot") else None,
        qualification_service.sheet_id,
        repository,
    )
    return workspace


async def sync_round_overview(bot, qualification_service, snapshot, embeds) -> None:
    workspace = await ensure_workspace(
        bot,
        qualification_service.sheet_id,
        qualification_service.registration_repository,
    )
    round_id = _text(snapshot.round_row.get("round_id"))
    tournament_id = _text(snapshot.round_row.get("tournament_id"))
    status = _text(snapshot.round_row.get("status")).lower()
    overview_id = _text(snapshot.round_row.get("overview_message_id"))
    current_resource = await workspace.repository.discord_resource(
        _GLOBAL_RESOURCE_ID, _CURRENT_RESOURCE_TYPE, "main"
    )

    if status == "closed":
        await _archive_round_message(
            workspace,
            tournament_id=tournament_id,
            round_id=round_id,
            embeds=embeds,
        )
        ids_to_delete = set()
        if overview_id:
            ids_to_delete.add(overview_id)
        if current_resource and _text(current_resource.get("notes")) == round_id:
            if _text(current_resource.get("message_id")):
                ids_to_delete.add(_text(current_resource.get("message_id")))
        for message_id in ids_to_delete:
            await _delete_message(workspace.parent, message_id)
        if current_resource and _text(current_resource.get("notes")) == round_id:
            await _set_current_resource(
                workspace,
                message_id=_text(current_resource.get("message_id")),
                round_id=round_id,
                state="retired",
            )
        await refresh_index(workspace, current_message=None)
        return

    if status not in _PUBLIC_ROUND_STATUSES:
        return

    message = None
    if (
        current_resource
        and _text(current_resource.get("state")) == "active"
        and _text(current_resource.get("notes")) == round_id
    ):
        message = await _fetch_message(
            workspace.parent, _text(current_resource.get("message_id"))
        )
    if message is None and overview_id:
        message = await _fetch_message(workspace.parent, overview_id)
    if message is None:
        message = await workspace.parent.send(embeds=list(embeds))
    else:
        await message.edit(embeds=list(embeds))

    if current_resource and _text(current_resource.get("notes")) not in {"", round_id}:
        await _delete_message(workspace.parent, _text(current_resource.get("message_id")))
    await _set_current_resource(
        workspace,
        message_id=str(message.id),
        round_id=round_id,
        state="active",
    )
    if overview_id != str(message.id):
        recorder = getattr(qualification_service, "record_overview_message_id", None)
        if callable(recorder):
            await recorder(round_id, str(message.id))
    await refresh_index(workspace, current_message=message)


async def _sync_round_discord(bot, qualification_service, snapshot) -> list[str]:
    """Existing result-control reconciliation plus workspace-aware overview routing."""
    from modules.community.live_arena import runtime_hooks
    from modules.community.live_arena.round_overview import render_round_overview_embeds

    warnings: list[str] = []
    sheet_id = qualification_service.sheet_id
    round_status = snapshot.status
    matches = [dict(row) for row in snapshot.matches]

    for match in matches:
        thread_id = _text(match.get("thread_id"))
        if not thread_id:
            continue
        try:
            thread = bot.get_channel(int(thread_id))
            if thread is None:
                thread = await bot.fetch_channel(int(thread_id))
            report_disabled = not (
                round_status
                in {
                    "active",
                    "published",
                    "open",
                    "published/open",
                    "correction_in_progress",
                }
                and _text(match.get("status")) in {"published", "open"}
            )
            dispute_disabled = not (
                round_status
                in {
                    "active",
                    "published",
                    "open",
                    "published/open",
                    "correction_in_progress",
                }
                and _text(match.get("status")) == "pending_confirmation"
            )
            await runtime_hooks._ensure_match_result_view(
                thread,
                sheet_id,
                report_disabled=report_disabled,
                dispute_disabled=dispute_disabled,
            )
        except Exception:
            log.exception(
                "Live Arena result-control reconciliation failed • match=%s",
                _text(match.get("match_id")),
            )
            warnings.append(
                f"Match {_text(match.get('match_number'))} result controls"
            )

    try:
        workspace = await ensure_workspace(
            bot,
            sheet_id,
            qualification_service.registration_repository,
        )
        guild_id = _text(getattr(getattr(workspace.parent, "guild", None), "id", ""))
        _, (_, tournament), _, _ = await qualification_service.context()
        standings = []
        if _text(snapshot.round_row.get("round_stage")).lower() == "qualification":
            competition_service = CompetitionResolutionService(sheet_id)
            await competition_service.initialize()
            standings = await competition_service.standings()
        embeds = await render_round_overview_embeds(
            sheet_id=sheet_id,
            tournament=tournament,
            round_row=snapshot.round_row,
            matches=matches,
            standings=standings,
            guild_id=guild_id,
        )
        await sync_round_overview(bot, qualification_service, snapshot, embeds)
    except Exception:
        log.exception("Live Arena Victory Ledger workspace synchronization failed")
        warnings.append("Victory Ledger overview")
    return warnings


async def _sync_hall_of_fame(manager) -> None:
    from modules.community.live_arena import hall_of_fame

    repository, _qrepo, (results, _) = await hall_of_fame._history_data(manager.sheet_id)
    if not results:
        return
    messages = await hall_of_fame._load_messages(
        manager.sheet_id, {"hall_of_fame_panel"}
    )
    embed = messages["hall_of_fame_panel"].embed(
        completed_count=len(results),
        tournament_lines=hall_of_fame._tournament_lines(results),
    )
    workspace = await ensure_workspace(manager.bot, manager.sheet_id, repository)
    target = workspace.hall_of_fame
    resource = await repository.discord_resource(
        _GLOBAL_RESOURCE_ID, "hall_of_fame", "main"
    )
    message = None
    old_message = None
    if resource and _text(resource.get("message_id")):
        target_id = _text(resource.get("thread_id")) or _text(resource.get("channel_id"))
        if target_id == str(target.id):
            message = await _fetch_message(target, _text(resource.get("message_id")))
        else:
            old_channel = await _resolve_channel(manager.bot, _text(resource.get("channel_id")))
            old_message = await _fetch_message(old_channel, _text(resource.get("message_id")))
    view = hall_of_fame.HallOfFameView(manager.sheet_id)
    if message is None:
        message = await target.send(embed=embed, view=view)
    else:
        await message.edit(embed=embed, view=view)
    now = _now()
    await repository.upsert_discord_resource(
        tournament_id=_GLOBAL_RESOURCE_ID,
        resource_type="hall_of_fame",
        resource_key="main",
        channel_id=str(target.id),
        message_id=str(message.id),
        thread_id=str(target.id),
        created_at_utc=_text(resource.get("created_at_utc")) if resource else now,
        updated_at_utc=now,
        state="active",
        notes="Cross-tournament Live Arena Hall of Fame",
    )
    if old_message is not None:
        try:
            await old_message.delete()
        except discord.NotFound:
            pass
    await refresh_index(workspace)


async def _sync_final_recap(manager, service, summary) -> None:
    """Route the existing factual recap into Tournament Results, preserving its logic."""
    workspace = await ensure_workspace(
        manager.bot,
        manager.sheet_id,
        service.registration_repository,
    )
    previous = _text(service.repository.config.get("ROUND_OVERVIEW_CHANNEL_ID"))
    service.repository.config["ROUND_OVERVIEW_CHANNEL_ID"] = str(workspace.results.id)
    try:
        await _original_final_recap(manager, service, summary)
    finally:
        service.repository.config["ROUND_OVERVIEW_CHANNEL_ID"] = previous
    await refresh_index(workspace)


async def _migrate_final_recaps(manager, workspace: Workspace) -> None:
    resources = await workspace.repository.discord_resources()
    for resource in resources:
        if _text(resource.get("resource_type")) != "final_recap":
            continue
        message_id = _text(resource.get("message_id"))
        if not message_id:
            continue
        target_id = _text(resource.get("thread_id")) or _text(resource.get("channel_id"))
        if target_id == str(workspace.results.id):
            continue
        old_channel = await _resolve_channel(manager.bot, _text(resource.get("channel_id")))
        old_message = await _fetch_message(old_channel, message_id)
        if old_message is None:
            continue
        copied = await workspace.results.send(embeds=list(old_message.embeds))
        await workspace.repository.upsert_discord_resource(
            tournament_id=_text(resource.get("tournament_id")),
            resource_type="final_recap",
            resource_key=_text(resource.get("resource_key")) or "main",
            channel_id=str(workspace.results.id),
            message_id=str(copied.id),
            thread_id=str(workspace.results.id),
            created_at_utc=_text(resource.get("created_at_utc")),
            updated_at_utc=_now(),
            state=_text(resource.get("state")) or "active",
            notes=_text(resource.get("notes")) or "Final tournament recap",
        )
        try:
            await old_message.delete()
        except discord.NotFound:
            pass


async def _fallback_round_embeds(
    manager,
    service,
    round_row,
    all_matches,
    tournament_by_id,
    guild_id: str,
):
    from modules.community.live_arena.round_overview import render_round_overview_embeds

    tid = _text(round_row.get("tournament_id"))
    round_id = _text(round_row.get("round_id"))
    matches = [row for row in all_matches if _text(row.get("round_id")) == round_id]
    standings = []
    if _text(round_row.get("round_stage")).lower() == "qualification":
        try:
            number = int(_text(round_row.get("round_number")) or 0)
        except ValueError:
            number = 0
        allowed = {f"{tid}-Q{index}" for index in range(1, number + 1)}
        relevant = [
            row
            for row in all_matches
            if _text(row.get("tournament_id")) == tid
            and _text(row.get("round_id")) in allowed
        ]
        standings = calculate_qualification_standings(relevant, tid)
    tournament = tournament_by_id.get(tid, {"tournament_name": tid})
    return await render_round_overview_embeds(
        sheet_id=manager.sheet_id,
        tournament=tournament,
        round_row=round_row,
        matches=matches,
        standings=standings,
        guild_id=guild_id,
    )


async def reconcile_workspace(manager) -> None:
    """One-time/startup migration of legacy main-channel history into the workspace."""
    from modules.community.live_arena import hall_of_fame
    from modules.community.live_arena.qualification import QualificationService

    service = QualificationService(manager.sheet_id)
    await service.initialize()
    workspace = await ensure_workspace(
        manager.bot, manager.sheet_id, service.registration_repository
    )
    await refresh_index(workspace)

    rounds = [dict(row) for row in await service.repository.rounds()]
    matches = [dict(row) for row in await service.repository.matches()]
    config = workspace.repository.config
    tournament_rows = _rows(
        await afetch_values(manager.sheet_id, config["TOURNAMENTS_TAB"]) or [],
        TOURNAMENT_HEADERS,
        config["TOURNAMENTS_TAB"],
    )
    tournament_by_id = {
        _text(row.get("tournament_id")): dict(row) for row in tournament_rows
    }
    guild_id = _text(getattr(getattr(workspace.parent, "guild", None), "id", ""))

    for round_row in rounds:
        if _text(round_row.get("status")).lower() != "closed":
            continue
        round_id = _text(round_row.get("round_id"))
        tid = _text(round_row.get("tournament_id"))
        legacy_id = _text(round_row.get("overview_message_id"))
        archive_resource = await workspace.repository.discord_resource(
            tid, _ARCHIVE_RESOURCE_TYPE, round_id
        )
        archived_message = None
        if archive_resource and (
            _text(archive_resource.get("thread_id")) == str(workspace.archive.id)
        ):
            archived_message = await _fetch_message(
                workspace.archive, _text(archive_resource.get("message_id"))
            )
        legacy_message = await _fetch_message(workspace.parent, legacy_id)
        if archived_message is None:
            embeds = list(legacy_message.embeds) if legacy_message is not None else []
            if not embeds:
                embeds = await _fallback_round_embeds(
                    manager,
                    service,
                    round_row,
                    matches,
                    tournament_by_id,
                    guild_id,
                )
            await _archive_round_message(
                workspace,
                tournament_id=tid,
                round_id=round_id,
                embeds=embeds,
            )
        if legacy_message is not None:
            try:
                await legacy_message.delete()
            except discord.NotFound:
                pass

    await _migrate_final_recaps(manager, workspace)
    await _sync_hall_of_fame(manager)

    active = [
        row
        for row in rounds
        if _text(row.get("status")).lower() in _PUBLIC_ROUND_STATUSES
        and _text(row.get("overview_message_id"))
    ]
    if active:
        active.sort(key=lambda row: _text(row.get("published_at_utc")))
        row = active[-1]
        message = await _fetch_message(
            workspace.parent, _text(row.get("overview_message_id"))
        )
        if message is not None:
            await _set_current_resource(
                workspace,
                message_id=str(message.id),
                round_id=_text(row.get("round_id")),
                state="active",
            )
            await refresh_index(workspace, current_message=message)
            return

    current_resource = await workspace.repository.discord_resource(
        _GLOBAL_RESOURCE_ID, _CURRENT_RESOURCE_TYPE, "main"
    )
    if current_resource and _text(current_resource.get("state")) == "active":
        await _delete_message(workspace.parent, _text(current_resource.get("message_id")))
        await _set_current_resource(
            workspace,
            message_id=_text(current_resource.get("message_id")),
            round_id=_text(current_resource.get("notes")),
            state="retired",
        )
    await refresh_index(workspace, current_message=None)



def _schedule_reconcile(manager) -> None:
    sheet_id = str(manager.sheet_id)
    existing = _reconcile_tasks.get(sheet_id)
    if existing is not None and not existing.done():
        return

    async def run():
        try:
            # Non-critical history migration deliberately runs late in startup so it
            # does not compete with live result recovery for the Sheets read budget.
            await asyncio.sleep(240)
            await reconcile_workspace(manager)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Live Arena Victory Ledger workspace startup migration failed")
        finally:
            if _reconcile_tasks.get(sheet_id) is asyncio.current_task():
                _reconcile_tasks.pop(sheet_id, None)

    try:
        _reconcile_tasks[sheet_id] = asyncio.create_task(
            run(), name=f"live-arena-victory-ledger:{sheet_id[-6:]}"
        )
    except RuntimeError:
        pass



def install() -> None:
    global _installed, _original_final_recap
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import (
        hall_of_fame,
        knockout_runtime,
        qualification_panel,
        runtime_hooks,
    )

    runtime_hooks._sync_round_discord = _sync_round_discord

    _original_final_recap = knockout_runtime._sync_final_recap
    knockout_runtime._sync_final_recap = _sync_final_recap
    hall_of_fame.sync_hall_of_fame = _sync_hall_of_fame

    original_install = qualification_panel.install_qualification

    def install_with_workspace(manager) -> bool:
        installed = original_install(manager)
        if not installed:
            return False
        if getattr(manager, "_victory_ledger_workspace_installed", False):
            return True
        manager._victory_ledger_workspace_installed = True
        _schedule_reconcile(manager)
        return True

    qualification_panel.install_qualification = install_with_workspace
