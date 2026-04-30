"""Public clan profile command registered under the cogs namespace."""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Dict, List, Optional

import discord
from discord.ext import commands

from c1c_coreops.helpers import help_metadata, tier

from modules.recruitment import cards, emoji_pipeline
from shared.sheets import async_facade as sheets

_VALID_TAG_CHARS: frozenset[str] = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
_FLIP_EMOJI = "\N{ELECTRIC LIGHT BULB}"  # 💡


@dataclass
class _FlipState:
    """State tracked for each toggleable clan profile message."""

    row: List[str]
    tag: str
    guild_id: Optional[int]
    channel_id: Optional[int]
    mode: str = "profile"
    crest_bytes: Optional[bytes] = None
    crest_filename: Optional[str] = None
    crest_static_url: Optional[str] = None


def _normalize_tag(raw: str | None) -> str:
    if raw is None:
        return ""
    filtered = [ch for ch in str(raw).upper() if ch in _VALID_TAG_CHARS]
    return "".join(filtered)


def _error_embed(tag: str) -> discord.Embed:
    description = f"Unknown clan tag `{tag}`."
    return discord.Embed(description=description, color=discord.Color.red())


async def _safe_delete(message: discord.Message | None) -> None:
    if not message:
        return
    try:
        await message.delete()
    except Exception:
        pass


class ClanProfileCog(commands.Cog):
    """Registers the public ``!clan`` profile command."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._flip_index: Dict[int, _FlipState] = {}

    @tier("user")
    @help_metadata(function_group="recruitment", section="recruitment", access_tier="user")
    @commands.command(
        name="clan",
        help="Shows a clan’s profile card by tag, including entry requirements and crest details.",
        brief="Shows a clan’s profile card by tag.",
        usage="clan <clantag>",
    )
    async def clan(self, ctx: commands.Context, tag: str) -> None:
        """Render a crest-enabled clan profile with a 💡 reaction toggle."""

        normalized = _normalize_tag(tag)
        if len(normalized) < 2:
            await ctx.reply(embed=_error_embed(tag or "?"), mention_author=False)
            return

        row = await sheets.get_clan_by_tag(normalized)
        if row is None:
            await ctx.reply(embed=_error_embed(normalized), mention_author=False)
            return

        row_tag = _normalize_tag(row[2] if len(row) > 2 else normalized) or normalized

        crest_bytes, crest_filename, crest_static_url = await self._load_crest(
            ctx.guild, row_tag
        )

        state = _FlipState(
            row=row,
            tag=row_tag,
            guild_id=ctx.guild.id if ctx.guild else None,
            channel_id=getattr(ctx.channel, "id", None),
            crest_bytes=crest_bytes,
            crest_filename=crest_filename,
            crest_static_url=crest_static_url,
        )

        profile_embed = self._build_profile_embed(state, ctx.guild)
        attachments = self._build_attachments(state)

        if attachments:
            message = await ctx.send(embed=profile_embed, files=attachments)
        else:
            message = await ctx.send(embed=profile_embed)

        self._flip_index[message.id] = state

        try:
            await message.add_reaction(_FLIP_EMOJI)
        except Exception:
            self._flip_index.pop(message.id, None)

        await _safe_delete(ctx.message)

    @commands.Cog.listener("on_raw_reaction_add")
    async def _on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if str(payload.emoji) != _FLIP_EMOJI:
            return
        if payload.user_id == getattr(self.bot.user, "id", None):
            return

        state = self._flip_index.get(payload.message_id)
        if state is None:
            return

        if state.guild_id and payload.guild_id != state.guild_id:
            return

        channel = self.bot.get_channel(payload.channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(payload.channel_id)
            except Exception:
                return

        try:
            message = await channel.fetch_message(payload.message_id)
        except Exception:
            self._flip_index.pop(payload.message_id, None)
            return

        guild = message.guild if isinstance(message.guild, discord.Guild) else None

        next_mode = "entry" if state.mode == "profile" else "profile"
        if next_mode == "profile":
            embed = self._build_profile_embed(state, guild)
        else:
            embed = self._build_entry_embed(state, guild)

        attachments = self._build_attachments(state)

        try:
            await message.edit(embed=embed, attachments=attachments, view=None)
        except Exception:
            return

        state.mode = next_mode

        member = payload.member
        if member is None and guild:
            member = guild.get_member(payload.user_id)

        if member is not None:
            try:
                await message.remove_reaction(payload.emoji, member)
            except Exception:
                pass
        else:
            try:
                user = self.bot.get_user(payload.user_id) or await self.bot.fetch_user(
                    payload.user_id
                )
            except Exception:
                user = None
            if user is not None:
                try:
                    await message.remove_reaction(payload.emoji, user)
                except Exception:
                    pass

    @commands.Cog.listener("on_raw_message_delete")
    async def _on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        self._flip_index.pop(payload.message_id, None)

    @commands.Cog.listener("on_raw_bulk_message_delete")
    async def _on_raw_bulk_message_delete(
        self, payload: discord.RawBulkMessageDeleteEvent
    ) -> None:
        for message_id in payload.message_ids:
            self._flip_index.pop(message_id, None)

    async def _load_crest(
        self,
        guild: discord.Guild | None,
        tag: str,
    ) -> tuple[Optional[bytes], Optional[str], Optional[str]]:
        if guild is None:
            return None, None, None

        crest_bytes: Optional[bytes] = None
        crest_filename: Optional[str] = None
        crest_static_url: Optional[str] = None

        size, box = emoji_pipeline.tag_badge_defaults()
        file, _ = await emoji_pipeline.build_tag_thumbnail(
            guild,
            tag,
            size=size,
            box=box,
        )
        if file and file.fp:
            try:
                file.fp.seek(0)
                crest_bytes = file.fp.read()
                crest_filename = file.filename or f"{tag.lower() or 'crest'}-badge.png"
            except Exception:
                crest_bytes = None
                crest_filename = None

        if crest_bytes is None:
            proxy_url = emoji_pipeline.padded_emoji_url(guild, tag)
            if proxy_url:
                crest_static_url = proxy_url
            elif not emoji_pipeline.is_strict_proxy_enabled():
                emoji = emoji_pipeline.emoji_for_tag(guild, tag)
                if emoji:
                    crest_static_url = str(emoji.url)

        return crest_bytes, crest_filename, crest_static_url

    def _crest_embed_url(self, state: _FlipState) -> Optional[str]:
        if state.crest_bytes and state.crest_filename:
            return f"attachment://{state.crest_filename}"
        return state.crest_static_url

    def _build_profile_embed(
        self,
        state: _FlipState,
        guild: discord.Guild | None,
    ) -> discord.Embed:
        embed = cards.make_embed_for_profile(state.row, guild=guild)
        crest_url = self._crest_embed_url(state)
        if crest_url:
            embed.set_thumbnail(url=crest_url)
        return embed

    def _build_entry_embed(
        self,
        state: _FlipState,
        guild: discord.Guild | None,
    ) -> discord.Embed:
        embed = cards.make_embed_for_row_search(state.row, filters_text="", guild=guild)
        crest_url = self._crest_embed_url(state)
        if crest_url:
            embed.set_thumbnail(url=crest_url)
        embed.set_footer(text="React with 💡 for Clan Profile")
        return embed

    def _build_attachments(self, state: _FlipState) -> List[discord.File]:
        if not state.crest_bytes or not state.crest_filename:
            return []
        buf = io.BytesIO(state.crest_bytes)
        buf.seek(0)
        return [discord.File(buf, filename=state.crest_filename)]


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ClanProfileCog(bot))
