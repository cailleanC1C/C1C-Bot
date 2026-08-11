"""Runtime reconciliation for Q2/Q3 previews and active Swiss rounds."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import discord

from modules.community.live_arena.messages import load_pr5_config
from modules.community.live_arena.result_views import set_post_mutation_sync
from modules.community.live_arena.service import _text, load_config
from modules.community.live_arena.swiss import (
    SwissQualificationService,
    _source_fingerprint_from_notes,
    source_fingerprint,
)
from modules.community.live_arena.swiss_panel import SwissPublisher, preview_embed

log = logging.getLogger("c1c.community.live_arena.swiss_runtime")
_installed = False
_PUBLIC = {
    "open",
    "active",
    "published",
    "published/open",
    "ready_to_close",
    "closed",
    "correction_in_progress",
}


def install() -> None:
    """Extend the installed organizer manager without changing the Sheet contract."""
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import qualification_panel

    original_install = qualification_panel.install_qualification

    def install_with_swiss_runtime(manager) -> bool:
        installed = original_install(manager)
        if not installed:
            return False
        if getattr(manager, "_swiss_runtime_installed", False):
            return True
        manager._swiss_runtime_installed = True

        base_competition_sync = getattr(manager, "_competition_sync", None)

        async def competition_sync():
            service = SwissQualificationService(manager.sheet_id)
            await service.initialize()
            warnings: list[str] = []
            for number in (3, 2):
                snapshot = await service.snapshot(number)
                if snapshot.round_row is not None and snapshot.status in _PUBLIC:
                    warnings.extend(
                        await SwissPublisher(manager.bot, service).reconcile(snapshot)
                    )
                    await _reconcile_preview(manager)
                    return list(dict.fromkeys(warnings))
            if callable(base_competition_sync):
                warnings.extend(await base_competition_sync())
            await _reconcile_preview(manager)
            return list(dict.fromkeys(warnings))

        manager._competition_sync = competition_sync
        set_post_mutation_sync(manager.sheet_id, competition_sync)

        base_sync = getattr(manager, "sync", None)
        if callable(base_sync):

            async def sync_with_swiss(*args, **kwargs):
                result = await base_sync(*args, **kwargs)
                try:
                    await _reconcile_preview(manager)
                except Exception:
                    log.exception("Live Arena Swiss preview reconciliation failed")
                return result

            manager.sync = sync_with_swiss
        return True

    qualification_panel.install_qualification = install_with_swiss_runtime


async def _reconcile_preview(manager) -> None:
    """Keep one preview current and auto-create the final preview after round closure."""
    service = SwissQualificationService(manager.sheet_id)
    await service.initialize()
    config = await load_config(manager.sheet_id)
    tid = config["ACTIVE_TOURNAMENT_ID"]
    rounds = [
        row
        for row in await service.repository.rounds()
        if _text(row.get("tournament_id")) == tid
    ]
    matches = await service.repository.matches()

    for number in (2, 3):
        previous = _round(rounds, number - 1)
        target = _round(rounds, number)
        status = _text(target.get("status")) if target else ""

        # An early organizer preview is live state, even while the current round is
        # still open. Finalized-result changes invalidate it immediately.
        if target is not None and status == "preview":
            expected = _source_fingerprint_from_notes(_text(target.get("notes")))
            current = source_fingerprint(matches, tid, before_round=number)
            if expected != current:
                try:
                    snapshot = await service.generate_preview(
                        "system", number, regenerate=True
                    )
                except Exception as exc:
                    await _post_preview_error(manager, number, exc)
                    return
            else:
                snapshot = await service.snapshot(number)
            await _sync_preview_message(manager, service, snapshot)
            return

        if target is not None and status in _PUBLIC:
            await _retire_preview_message(manager, service, number)
            continue

        if previous is None or _text(previous.get("status")) != "closed":
            continue

        # Closure is the final-draw trigger if no preview exists yet.
        if target is None:
            try:
                snapshot = await service.generate_preview("system", number)
            except Exception as exc:
                await _post_preview_error(manager, number, exc)
                return
            await _sync_preview_message(manager, service, snapshot)
            return


async def _sync_preview_message(manager, service, snapshot) -> None:
    number = int(_text(snapshot.round_row.get("round_number")))
    config, _ = await load_pr5_config(manager.sheet_id)
    channel_id = _text(config.get("ORGANIZER_CHANNEL_ID"))
    if not channel_id:
        raise RuntimeError("CONFIG: missing ORGANIZER_CHANNEL_ID for Swiss preview")
    channel = manager.bot.get_channel(int(channel_id))
    if channel is None:
        channel = await manager.bot.fetch_channel(int(channel_id))

    resource = await service.registration_repository.discord_resource(
        _text(snapshot.round_row["tournament_id"]), "swiss_preview", f"q{number}"
    )
    message = None
    if (
        resource
        and _text(resource.get("state")) == "active"
        and _text(resource.get("message_id"))
    ):
        try:
            message = await channel.fetch_message(int(_text(resource["message_id"])))
        except discord.NotFound:
            message = None
    embed = preview_embed(snapshot, official=False)
    if message is None:
        message = await channel.send(embed=embed)
    else:
        await message.edit(embed=embed)

    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    await service.registration_repository.upsert_discord_resource(
        tournament_id=_text(snapshot.round_row["tournament_id"]),
        resource_type="swiss_preview",
        resource_key=f"q{number}",
        channel_id=channel_id,
        message_id=str(message.id),
        created_at_utc=(_text(resource.get("created_at_utc")) if resource else now),
        updated_at_utc=now,
        state="active",
        notes="Organizer-only Swiss preview; not official until approved/published",
    )


async def _retire_preview_message(manager, service, number: int) -> None:
    config = await load_config(manager.sheet_id)
    tid = config["ACTIVE_TOURNAMENT_ID"]
    resource = await service.registration_repository.discord_resource(
        tid, "swiss_preview", f"q{number}"
    )
    if not resource or _text(resource.get("state")) != "active":
        return
    channel_id = _text(resource.get("channel_id"))
    message_id = _text(resource.get("message_id"))
    if channel_id and message_id:
        try:
            channel = manager.bot.get_channel(int(channel_id))
            if channel is None:
                channel = await manager.bot.fetch_channel(int(channel_id))
            message = await channel.fetch_message(int(message_id))
            await message.delete()
        except discord.NotFound:
            pass
        except Exception:
            log.exception("Live Arena Swiss preview deletion failed")
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    await service.registration_repository.upsert_discord_resource(
        tournament_id=tid,
        resource_type="swiss_preview",
        resource_key=f"q{number}",
        channel_id=channel_id,
        message_id=message_id,
        created_at_utc=_text(resource.get("created_at_utc")),
        updated_at_utc=now,
        state="retired",
        notes="Preview retired after official round publication",
    )


async def _post_preview_error(manager, number: int, exc: Exception) -> None:
    try:
        config, _ = await load_pr5_config(manager.sheet_id)
        channel_id = _text(config.get("ORGANIZER_CHANNEL_ID"))
        if not channel_id:
            return
        channel = manager.bot.get_channel(int(channel_id))
        if channel is None:
            channel = await manager.bot.fetch_channel(int(channel_id))
        await channel.send(
            embed=discord.Embed(
                title=f"Q{number} Swiss draw needs organizer review",
                description=str(exc)[:4000],
                color=0x3498DB,
            )
        )
    except Exception:
        log.exception("Live Arena Swiss conflict notice failed")


def _round(rounds, number: int):
    found = [
        row
        for row in rounds
        if _text(row.get("round_stage")).lower() == "qualification"
        and _text(row.get("round_number")) == str(number)
    ]
    return found[0] if len(found) == 1 else None
