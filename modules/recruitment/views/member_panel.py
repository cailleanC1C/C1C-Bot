"""Member-facing clan search controller and helpers."""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

import discord
from discord import Message
from discord.ext import commands

from modules.recruitment import search as roster_search
from modules.recruitment.search_helpers import format_filters_footer
from modules.recruitment.views.shared import MemberSearchPagedView
from shared import config as shared_config
from shared.sheets.recruitment import RecruitmentClanRecord

fetch_clans_async = roster_search.fetch_roster_records

log = logging.getLogger("c1c.recruitment.member")

NONE = discord.AllowedMentions.none()

MAX_VISIBLE_ROWS = 5


def _build_empty_embed(filters_text: str | None = None) -> discord.Embed:
    embed = discord.Embed(
        title="No matching clans found.",
        description="Try adjusting your filters and search again.",
    )
    if filters_text:
        embed.set_footer(text=f"Filters used: {filters_text}")
    return embed


@dataclass
class MemberSearchFilters:
    """Snapshot of all clan-search filters selected by the member."""

    cb: str | None = None
    hydra: str | None = None
    chimera: str | None = None
    cvc: str | None = None
    siege: str | None = None
    playstyle: str | None = None
    roster_mode: str | None = "open"

    def any_active(self) -> bool:
        return any(
            value is not None and str(value).strip()
            for value in (
                self.cb,
                self.hydra,
                self.chimera,
                self.cvc,
                self.siege,
                self.playstyle,
            )
        ) or self.roster_mode is not None

    def copy(self) -> "MemberSearchFilters":
        return MemberSearchFilters(
            cb=self.cb,
            hydra=self.hydra,
            chimera=self.chimera,
            cvc=self.cvc,
            siege=self.siege,
            playstyle=self.playstyle,
            roster_mode=self.roster_mode,
        )


# Track the active results message per (guild_id, channel_id, user_id)
ACTIVE_PANELS: dict[tuple[int, int, int], int] = {}


def _coerce_filters(raw: MemberSearchFilters | Mapping[str, object]) -> MemberSearchFilters:
    if isinstance(raw, MemberSearchFilters):
        return raw

    data = dict(raw)
    return MemberSearchFilters(
        cb=data.get("cb") or None,
        hydra=data.get("hydra") or None,
        chimera=data.get("chimera") or None,
        cvc=data.get("cvc") or None,
        siege=data.get("siege") or None,
        playstyle=data.get("playstyle") or None,
        roster_mode=data.get("roster_mode") or None,
    )


class MemberPanelController:
    """Manage the lifecycle of member search results in a single message."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def open_or_reuse(self, ctx: commands.Context) -> None:
        """Ensure a results message exists for ``ctx.author`` in this channel."""

        filters = MemberSearchFilters()
        await self.update_results(ctx, filters=filters)

    async def update_results(
        self,
        ctx: commands.Context | None,
        *,
        filters: MemberSearchFilters | Mapping[str, object],
        interaction: discord.Interaction | None = None,
    ) -> None:
        """Rebuild the member search results based on ``filters``.

        ``ctx`` may be ``None`` when invoked from an interaction callback; in that
        case an ``interaction`` must be provided.
        """

        if ctx is None and interaction is None:
            raise ValueError("either ctx or interaction must be provided")

        filters = _coerce_filters(filters).copy()

        channel = None
        guild = None
        author_id: int | None = None

        if ctx is not None:
            channel = getattr(ctx, "channel", None)
            guild = getattr(ctx, "guild", None)
            author = getattr(ctx, "author", None)
            if author is not None:
                author_id = getattr(author, "id", None)

        if interaction is not None:
            if interaction.channel is not None:
                channel = interaction.channel
            if interaction.guild is not None:
                guild = interaction.guild
            if interaction.user is not None:
                author_id = getattr(interaction.user, "id", author_id)

        if channel is None or author_id is None:
            return

        channel_id = getattr(channel, "id", None)
        if channel_id is None:
            return

        guild_id = getattr(guild, "id", 0) or 0
        key = (int(guild_id), int(channel_id), int(author_id))

        message = await self._fetch_existing_message(channel, key)

        records = await self._load_rows()
        matches = self._filter_rows(records, filters)
        matches = roster_search.enforce_inactives_only(
            matches,
            filters.roster_mode,
            context="member_panel:pre_pagination",
        )

        soft_cap = max(1, shared_config.get_search_results_soft_cap(25))
        visible_cap = min(soft_cap, MAX_VISIBLE_ROWS)
        total_found = len(matches)
        visible_rows = matches[:visible_cap]

        cap_note = None
        if total_found > visible_cap:
            cap_note = f"first {visible_cap} of {total_found}"

        filters_text = format_filters_footer(
            filters.cb,
            filters.hydra,
            filters.chimera,
            filters.cvc,
            filters.siege,
            filters.playstyle,
            filters.roster_mode,
            extra_note=cap_note,
        )

        view = MemberSearchPagedView(
            author_id=int(author_id),
            rows=visible_rows,
            filters_text=filters_text,
            guild=guild,
            has_results=bool(visible_rows),
        )
        if not visible_rows:
            embed = _build_empty_embed(filters_text)
            await self._send_or_edit(
                channel,
                key,
                message,
                content=None,
                embeds=[embed],
                files=[],
                view=view,
                interaction=interaction,
                ctx=ctx,
            )
            return

        embeds, files = await view.build_outputs()

        await self._send_or_edit(
            channel,
            key,
            message,
            content=None,
            embeds=embeds,
            files=files,
            view=view,
            interaction=interaction,
            ctx=ctx,
        )

    async def _fetch_existing_message(
        self,
        channel,
        key: tuple[int, int, int],
    ) -> Message | None:
        message_id = ACTIVE_PANELS.get(key)
        if not message_id:
            return None

        fetcher = getattr(channel, "fetch_message", None)
        if fetcher is None:
            ACTIVE_PANELS.pop(key, None)
            return None

        try:
            message = await fetcher(message_id)
        except discord.NotFound:
            ACTIVE_PANELS.pop(key, None)
            return None
        except discord.Forbidden:
            ACTIVE_PANELS.pop(key, None)
            log.warning(
                "member clansearch cannot fetch message due to permissions", extra={"key": key}
            )
            return None
        except discord.HTTPException:
            log.exception("member clansearch failed to fetch message", extra={"key": key})
            return None

        return message

    async def _send_or_edit(
        self,
        channel,
        key: tuple[int, int, int],
        existing: Message | None,
        *,
        content: str | None,
        embeds: Sequence[discord.Embed],
        files: Iterable[discord.File],
        view: discord.ui.View | None,
        interaction: discord.Interaction | None,
        ctx: commands.Context | None,
    ) -> None:
        attachments = list(files)
        embed_list = list(embeds) if embeds is not None else []
        filters_text = getattr(view, "filters_text", None)
        safe_embeds = embed_list if embed_list else [_build_empty_embed(filters_text)]

        def close_attachments() -> None:
            for file in attachments:
                try:
                    file.close()
                except Exception:
                    pass

        if existing is None:
            if interaction is not None and not interaction.response.is_done():
                send = interaction.response.send_message
            elif interaction is not None:
                send = interaction.followup.send
            else:
                send = None
                if ctx is not None:
                    reply_fn = getattr(ctx, "reply", None)
                    if reply_fn is not None:

                        async def _reply_wrapper(*args, **kwargs):
                            kwargs.setdefault("mention_author", False)
                            return await reply_fn(*args, **kwargs)

                        send = _reply_wrapper
                if send is None:
                    send = getattr(channel, "send", None)
            if send is None:
                close_attachments()
                return
            try:
                sent = await send(
                    content=content,
                    embeds=safe_embeds,
                    files=attachments,
                    view=view,
                    allowed_mentions=NONE,
                )
            except discord.Forbidden as exc:
                close_attachments()
                await self._handle_forbidden(key, ctx, interaction, channel, exc)
                return
            except discord.HTTPException:
                close_attachments()
                log.exception(
                    "member clansearch send/edit failed",
                    extra=self._log_extra(ctx, interaction, key),
                )
                return
            close_attachments()
            if hasattr(sent, "id"):
                self._register_panel_key(
                    key,
                    getattr(sent, "id"),
                    ctx=ctx,
                    interaction=interaction,
                )
                if isinstance(view, MemberSearchPagedView):
                    view.message = sent  # type: ignore[assignment]
            return

        try:
            params = inspect.signature(existing.edit).parameters
        except (TypeError, ValueError):
            params = {}
        edit_kwargs = {
            "content": content,
            "embeds": safe_embeds,
            "attachments": attachments,
            "view": view,
        }
        if "allowed_mentions" in params:
            edit_kwargs["allowed_mentions"] = NONE

        try:
            await existing.edit(**edit_kwargs)
        except discord.Forbidden as exc:
            close_attachments()
            await self._handle_forbidden(key, ctx, interaction, channel, exc)
            return
        except discord.HTTPException:
            close_attachments()
            log.exception(
                "member clansearch send/edit failed",
                extra=self._log_extra(ctx, interaction, key),
            )
            return
        else:
            close_attachments()
            self._register_panel_key(
                key,
                existing.id,
                ctx=ctx,
                interaction=interaction,
            )
            if isinstance(view, MemberSearchPagedView):
                view.message = existing

    def _log_extra(
        self,
        ctx: commands.Context | None,
        interaction: discord.Interaction | None,
        key: tuple[int, int, int],
    ) -> dict[str, object]:
        guild_id, channel_id, user_id = key
        extra: dict[str, object] = {
            "guild": guild_id,
            "channel": channel_id,
            "user": user_id,
        }
        if ctx is not None:
            extra.update(
                {
                    "ctx_guild": ctx.guild.id if ctx.guild else None,
                    "ctx_channel": getattr(ctx.channel, "id", None),
                    "ctx_user": getattr(ctx.author, "id", None),
                }
            )
        if interaction is not None:
            extra.update(
                {
                    "interaction_guild": interaction.guild_id,
                    "interaction_channel": interaction.channel_id,
                    "interaction_user": getattr(interaction.user, "id", None),
                }
            )
        return extra

    async def _handle_forbidden(
        self,
        key: tuple[int, int, int],
        ctx: commands.Context | None,
        interaction: discord.Interaction | None,
        channel,
        exc: discord.Forbidden,
    ) -> None:
        ACTIVE_PANELS.pop(key, None)
        log.warning(
            "member clansearch send/edit forbidden",
            extra={
                **self._log_extra(ctx, interaction, key),
                "exception": repr(exc),
            },
        )
        text = (
            "I can’t post the search panel here (missing permissions like **Embed Links** or "
            "**Use External Emojis**). Ask staff to adjust channel perms, or try in #bot-test."
        )

        sender = None
        if ctx is not None and getattr(ctx.channel, "send", None) is not None:
            sender = ctx.channel.send
        elif interaction is not None:
            if not interaction.response.is_done():
                sender = interaction.response.send_message
            else:
                sender = interaction.followup.send
        elif getattr(channel, "send", None) is not None:
            sender = channel.send

        if sender is None:
            return

        try:
            await sender(text, allowed_mentions=NONE)
        except Exception:
            return

    def _register_panel_key(
        self,
        key: tuple[int, int, int],
        message_id: int,
        *,
        ctx: commands.Context | None,
        interaction: discord.Interaction | None,
    ) -> None:
        ACTIVE_PANELS[key] = message_id
        guild_id, channel_id, user_id = key
        log.debug(
            "member clansearch panel registered",
            extra={
                "guild": guild_id,
                "channel": channel_id,
                "user": user_id,
                "message": message_id,
                "ctx_user": getattr(ctx.author, "id", None) if ctx else None,
                "interaction_user": getattr(getattr(interaction, "user", None), "id", None),
            },
        )

    async def _load_rows(self) -> list[RecruitmentClanRecord]:
        try:
            records = await fetch_clans_async(force=False)
        except Exception:  # pragma: no cover - defensive guard
            log.exception("failed to fetch member clan rows")
            return []
        return roster_search.normalize_records(list(records))

    def _filter_rows(
        self, records: Sequence[RecruitmentClanRecord], filters: MemberSearchFilters
    ) -> list[RecruitmentClanRecord]:
        matches, diagnostics = roster_search.filter_records_with_diagnostics(
            records,
            cb=filters.cb,
            hydra=filters.hydra,
            chimera=filters.chimera,
            cvc=filters.cvc,
            siege=filters.siege,
            playstyle=filters.playstyle,
            roster_mode=filters.roster_mode,
        )
        if not matches:
            log.debug(
                "member clansearch returned no rows",
                extra={"filters": filters.__dict__, "diagnostics": diagnostics},
            )
        return matches
