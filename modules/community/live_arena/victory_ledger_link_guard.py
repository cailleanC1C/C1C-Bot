"""Keep Victory Ledger matchup links friendly after every Discord sync."""

from __future__ import annotations

import logging

import discord

from modules.community.live_arena.overview_links import match_thread_link
from modules.community.live_arena.service import _text

log = logging.getLogger("c1c.community.live_arena.victory_ledger_link_guard")
_installed = False


def install() -> None:
    """Install one final post-sync normalizer for every competition overview refresh."""
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import runtime_hooks

    original_sync = runtime_hooks._sync_round_discord

    async def sync_with_friendly_links(bot, qualification_service, snapshot):
        warnings = list(await original_sync(bot, qualification_service, snapshot))
        try:
            await _normalize_overview_links(bot, qualification_service, snapshot)
        except Exception:
            log.exception("Live Arena Victory Ledger final link normalization failed")
            warnings.append("Victory Ledger match links")
        return list(dict.fromkeys(warnings))

    runtime_hooks._sync_round_discord = sync_with_friendly_links


async def _normalize_overview_links(bot, service, snapshot) -> None:
    """Rewrite only matchup fields, preserving result state and standings already rendered."""
    round_row = getattr(snapshot, "round_row", None)
    if round_row is None:
        return
    overview_id = _text(round_row.get("overview_message_id"))
    if not overview_id:
        return

    config = service.repository.config
    channel_id = int(config["ROUND_OVERVIEW_CHANNEL_ID"])
    channel = bot.get_channel(channel_id)
    if channel is None:
        channel = await bot.fetch_channel(channel_id)
    guild_id = _text(getattr(getattr(channel, "guild", None), "id", ""))
    if not guild_id:
        return

    message = await channel.fetch_message(int(overview_id))
    embeds = list(getattr(message, "embeds", ()) or ())
    if not embeds:
        return
    embed = discord.Embed.from_dict(embeds[0].to_dict())

    fields_by_name = {
        str(field.name): index for index, field in enumerate(embed.fields)
    }
    changed = False
    for match in sorted(
        snapshot.matches,
        key=lambda row: int(_text(row.get("match_number")) or 0),
    ):
        match_number = _text(match.get("match_number"))
        index = fields_by_name.get(f"Match {match_number}")
        if index is None:
            continue
        field = embed.fields[index]
        thread_id = _text(match.get("thread_id"))
        friendly = match_thread_link(thread_id, guild_id)
        raw = f"<#{thread_id}>" if thread_id else "Forum post pending"
        value = str(field.value)
        if raw in value:
            value = value.replace(raw, friendly)
        elif thread_id and "Open match thread" not in value:
            value = f"{value.rstrip()}\n{friendly}"
        else:
            continue
        embed.set_field_at(
            index,
            name=field.name,
            value=value,
            inline=field.inline,
        )
        changed = True

    if changed:
        await message.edit(embed=embed)
