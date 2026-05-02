"""Legacy member search panel restored for ``!clansearch``."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Iterable, Sequence

import discord
from discord import InteractionResponded
from discord.ext import commands

from modules.recruitment import search as roster_search
from modules.recruitment.search_helpers import format_filters_footer
from modules.recruitment.views.filters_member import MemberFiltersView
from modules.recruitment.views.interaction_utils import defer_once
from modules.recruitment.views.shared_member import MemberSearchPagedView
from shared import config as shared_config
from shared.sheets.recruitment import RecruitmentClanRecord

fetch_clans_async = roster_search.fetch_roster_records

log = logging.getLogger("c1c.recruitment.member")

ALLOWED_MENTIONS = discord.AllowedMentions.none()

PANEL_KEY_VARIANT = "search"

# Track active panel message per user (legacy behavior)
ACTIVE_PANELS: dict[tuple[int, str], int] = {}

# Track the most recent results message posted per user/channel.
ACTIVE_RESULTS: dict[tuple[int, int, int], int] = {}

@dataclass
class MemberPanelState:
    """Snapshot of filter + view state for the legacy member panel."""

    author_id: int
    cb: str | None = None
    hydra: str | None = None
    chimera: str | None = None
    cvc: str | None = None
    siege: str | None = None
    playstyle: str | None = None
    roster_mode: str | None = "open"

    def copy(self) -> "MemberPanelState":
        return replace(self)

    def with_updates(self, **changes) -> "MemberPanelState":
        if "roster_mode" in changes and changes["roster_mode"] in {"", "any"}:
            changes["roster_mode"] = None
        return replace(self, **changes)

    def any_filters(self) -> bool:
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
        ) or self.roster_mode not in {None, "", "any"}


class MemberPanelControllerLegacy:
    """Restore the legacy new-message-per-search member clan search."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._panel_states: dict[int, MemberPanelState] = {}

    async def open(self, ctx: commands.Context) -> None:
        channel = getattr(ctx, "channel", None)
        author = getattr(ctx, "author", None)
        if channel is None or author is None:
            return

        author_id = getattr(author, "id", None)
        if author_id is None:
            return

        key = (int(author_id), PANEL_KEY_VARIANT)
        embed = self._build_intro_embed(author)

        existing_id = ACTIVE_PANELS.get(key)
        if existing_id:
            try:
                message = await channel.fetch_message(existing_id)
            except Exception:
                ACTIVE_PANELS.pop(key, None)
            else:
                state = self._panel_states.get(existing_id, MemberPanelState(author_id=author_id)).copy()
                view = MemberFiltersView(controller=self, state=state)
                view.bind_to_message(message)
                self._panel_states[message.id] = state
                try:
                    await message.edit(embed=embed, view=view)
                except discord.HTTPException:
                    log.exception("member panel reuse failed", extra={"message_id": message.id})
                return

        state = MemberPanelState(author_id=author_id)
        view = MemberFiltersView(controller=self, state=state)

        try:
            sent = await ctx.reply(embed=embed, view=view, mention_author=False)
        except discord.Forbidden as exc:
            log.warning("member panel send forbidden", exc_info=exc)
            await self._send_permission_fallback(ctx)
            return
        except discord.HTTPException as exc:
            log.exception("member panel send failed", exc_info=exc)
            return

        if isinstance(sent, discord.Message):
            view.bind_to_message(sent)
            self._panel_states[sent.id] = state
            ACTIVE_PANELS[key] = sent.id

    async def on_search(
        self,
        interaction: discord.Interaction,
        view: MemberFiltersView,
    ) -> None:
        state = view.state
        if not state.any_filters():
            await self._send_need_filter(interaction)
            return

        await defer_once(interaction, thinking=True)

        rows = await self._load_rows()
        matches = self._filter_rows(rows, state)

        total_found = len(matches)
        soft_cap = max(1, shared_config.get_search_results_soft_cap(25))
        cap_note = None
        if total_found > soft_cap:
            matches = matches[:soft_cap]
            cap_note = f"first {soft_cap} of {total_found}"

        filters_text = format_filters_footer(
            state.cb,
            state.hydra,
            state.chimera,
            state.cvc,
            state.siege,
            state.playstyle,
            state.roster_mode,
            extra_note=cap_note,
        )

        empty_embed = self._empty_results_embed(filters_text)

        results_view = MemberSearchPagedView(
            author_id=state.author_id,
            rows=matches,
            filters_text=filters_text,
            guild=interaction.guild,
            timeout=900,
            mode="lite",
            page=0,
            empty_embed=empty_embed,
        )
        results_view.state = state.copy()

        embeds, files = await results_view.build_outputs()

        guild_id = getattr(interaction.guild, "id", 0) or 0
        channel_id = getattr(getattr(interaction, "channel", None), "id", None)
        user_id = getattr(getattr(interaction, "user", None), "id", state.author_id)
        key = (int(guild_id), int(channel_id or 0), int(user_id))

        existing = await self._fetch_existing_results_message(interaction, key)
        await self._send_or_edit_results(
            interaction,
            key,
            existing_message=existing,
            embeds=embeds,
            files=files,
            view=results_view,
            empty_embed=empty_embed,
        )

    async def on_reset(self, interaction: discord.Interaction, view: MemberFiltersView) -> None:
        new_state = MemberPanelState(author_id=view.state.author_id)
        view.set_state(new_state)
        panel_id = view.panel_message_id
        if panel_id is not None:
            self._panel_states[panel_id] = new_state
        try:
            await interaction.response.edit_message(view=view)
        except InteractionResponded:
            try:
                if interaction.message is not None:
                    await interaction.followup.edit_message(
                        message_id=interaction.message.id,
                        view=view,
                    )
            except Exception:
                pass

    def update_panel_state(self, message_id: int | None, state: MemberPanelState) -> None:
        if message_id is None:
            return
        self._panel_states[message_id] = state

    async def _fetch_existing_results_message(
        self,
        interaction: discord.Interaction,
        key: tuple[int, int, int],
    ) -> discord.Message | None:
        message_id = ACTIVE_RESULTS.get(key)
        if not message_id:
            return None

        channel = getattr(interaction, "channel", None)
        fetcher = getattr(channel, "fetch_message", None)
        if fetcher is None:
            ACTIVE_RESULTS.pop(key, None)
            return None

        try:
            message = await fetcher(message_id)
        except discord.NotFound:
            ACTIVE_RESULTS.pop(key, None)
            return None
        except discord.Forbidden:
            ACTIVE_RESULTS.pop(key, None)
            log.warning("member results fetch forbidden", extra={"key": key})
            return None
        except discord.HTTPException:
            ACTIVE_RESULTS.pop(key, None)
            log.exception("member results fetch failed", extra={"key": key})
            return None

        return message

    async def _handle_results_forbidden(
        self,
        interaction: discord.Interaction,
        key: tuple[int, int, int],
        exc: discord.Forbidden,
    ) -> None:
        ACTIVE_RESULTS.pop(key, None)
        log.warning(
            "member results send/edit forbidden",
            extra={"key": key, "exception": repr(exc)},
        )

        notice = (
            "I can’t post the search results here (missing permissions like **Embed Links** or "
            "**Use External Emojis**). Ask staff to adjust channel perms, or try in #bot-test."
        )

        sender = None
        if not interaction.response.is_done():
            sender = interaction.response.send_message
        else:
            sender = interaction.followup.send

        if sender is None:
            return

        try:
            await sender(notice, allowed_mentions=ALLOWED_MENTIONS)
        except Exception:
            pass

    async def _notify_results_failure(
        self, interaction: discord.Interaction, key: tuple[int, int, int]
    ) -> None:
        log.exception("member results post/edit failed", extra={"key": key})
        note = "⚠️ I couldn’t post the clan search results. Try again in a moment."

        sender = None
        if not interaction.response.is_done():
            sender = interaction.response.send_message
        else:
            sender = interaction.followup.send

        if sender is None:
            return

        try:
            await sender(note, allowed_mentions=ALLOWED_MENTIONS)
        except Exception:
            pass

    async def _send_or_edit_results(
        self,
        interaction: discord.Interaction,
        key: tuple[int, int, int],
        *,
        existing_message: discord.Message | None,
        embeds: Sequence[discord.Embed],
        files: Iterable[discord.File],
        view: MemberSearchPagedView,
        empty_embed: discord.Embed,
    ) -> None:
        embed_list = list(embeds) if embeds else []
        safe_embeds = embed_list if embed_list else [empty_embed]
        attachments = list(files or [])

        while True:
            if existing_message is not None:
                existing_id = getattr(existing_message, "id", None)
                if existing_id is None:
                    ACTIVE_RESULTS.pop(key, None)
                    existing_message = None
                    continue
                try:
                    edited = await existing_message.edit(
                        content=None,
                        embeds=safe_embeds,
                        attachments=attachments,
                        view=view,
                        allowed_mentions=ALLOWED_MENTIONS,
                    )
                except discord.NotFound:
                    ACTIVE_RESULTS.pop(key, None)
                    _rewind_files(attachments)
                    existing_message = None
                    continue
                except discord.Forbidden as exc:
                    _close_files(attachments)
                    await self._handle_results_forbidden(interaction, key, exc)
                    return
                except discord.HTTPException:
                    _close_files(attachments)
                    ACTIVE_RESULTS.pop(key, None)
                    await self._notify_results_failure(interaction, key)
                    return
                else:
                    _close_files(attachments)
                    target_message = (
                        edited if isinstance(edited, discord.Message) else existing_message
                    )
                    try:
                        message_id = int(getattr(target_message, "id", None))
                    except (TypeError, ValueError):
                        message_id = None
                    if message_id is not None:
                        ACTIVE_RESULTS[key] = message_id
                    if isinstance(target_message, discord.Message):
                        view.bind_message(target_message)
                    else:
                        try:
                            view.bind_message(existing_message)
                        except Exception:
                            pass
                    await _acknowledge_refresh(interaction)
                    return

            try:
                sent = await interaction.followup.send(
                    content=None,
                    embeds=safe_embeds,
                    files=attachments,
                    view=view,
                    allowed_mentions=ALLOWED_MENTIONS,
                )
            except discord.Forbidden as exc:
                _close_files(attachments)
                await self._handle_results_forbidden(interaction, key, exc)
                return
            except discord.HTTPException:
                _close_files(attachments)
                ACTIVE_RESULTS.pop(key, None)
                await self._notify_results_failure(interaction, key)
                return
            else:
                _close_files(attachments)
                message_id = getattr(sent, "id", None)
                if message_id is not None:
                    try:
                        ACTIVE_RESULTS[key] = int(message_id)
                    except (TypeError, ValueError):
                        pass
                if isinstance(sent, discord.Message):
                    view.bind_message(sent)
                else:
                    try:
                        view.bind_message(sent)  # type: ignore[arg-type]
                    except Exception:
                        pass
                return

    async def _load_rows(self) -> Sequence[RecruitmentClanRecord]:
        try:
            rows = await fetch_clans_async(force=False)
        except Exception as exc:
            log.exception("member panel sheets fetch failed", exc_info=exc)
            return []
        return roster_search.normalize_records(list(rows or []))

    def _filter_rows(
        self, rows: Sequence[RecruitmentClanRecord], state: MemberPanelState
    ) -> list[RecruitmentClanRecord]:
        roster_mode = state.roster_mode
        if roster_mode in {"", "any"}:
            roster_mode = None

        matches, diagnostics = roster_search.filter_records_with_diagnostics(
            rows,
            cb=state.cb,
            hydra=state.hydra,
            chimera=state.chimera,
            cvc=state.cvc,
            siege=state.siege,
            playstyle=state.playstyle,
            roster_mode=roster_mode,
        )
        if not matches:
            log.debug(
                "member legacy clansearch returned no rows",
                extra={"state": state.__dict__, "diagnostics": diagnostics},
            )

        return roster_search.enforce_inactives_only(
            matches,
            roster_mode,
            context="member_panel_legacy:pre_pagination",
        )

    def _empty_results_embed(self, filters_text: str | None) -> discord.Embed:
        embed = discord.Embed(
            title="No matching clans found.",
            description="Try adjusting your filters and search again.",
        )
        if filters_text:
            embed.set_footer(text=f"Filters used: {filters_text}")
        return embed

    async def _send_permission_fallback(self, ctx: commands.Context) -> None:
        message = (
            "⚠️ I need the **Embed Links** permission in this channel to show the clan search panel."
        )
        try:
            await ctx.send(message)
        except Exception:
            pass

    async def _send_need_filter(self, interaction: discord.Interaction) -> None:
        note = "Pick at least **one** filter, then try again. 🙂"
        try:
            await interaction.response.send_message(note, ephemeral=True)
        except InteractionResponded:
            try:
                await interaction.followup.send(note, ephemeral=True)
            except Exception:
                pass

    def _build_intro_embed(self, author) -> discord.Embed:
        mention = getattr(author, "mention", None) or "member"
        description = (
            f"Hi {mention}!\n\n"
            "Pick any filters *(you can leave some blank)* and click **Search Clans** "
            "to see Entry Criteria and open spots."
        )
        embed = discord.Embed(
            title="Search for a C1C Clan",
            description=description,
        )
        embed.set_footer(text="Only the summoner can use this panel.")
        return embed


def _close_files(files: Iterable[discord.File]) -> None:
    for file in files:
        try:
            file.close()
        except Exception:
            pass


def _rewind_files(files: Iterable[discord.File]) -> None:
    for file in files:
        try:
            file.reset(seek=0)
        except Exception:
            try:
                if hasattr(file, "fp") and hasattr(file.fp, "seek"):
                    file.fp.seek(0)
            except Exception:
                pass


async def _acknowledge_refresh(interaction: discord.Interaction) -> None:
    try:
        await interaction.followup.send(
            "Search results updated.",
            ephemeral=True,
            allowed_mentions=ALLOWED_MENTIONS,
        )
    except Exception:
        pass


__all__ = [
    "ACTIVE_PANELS",
    "MemberPanelControllerLegacy",
    "MemberPanelState",
]
