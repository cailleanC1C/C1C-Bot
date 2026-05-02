"""Recruiter panel command restored from the legacy Matchmaker bot.

This module provides a text-only implementation of the `!clanmatch` panel that
mirrors the legacy filters while skipping crest rendering for mobile
performance.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from typing import TYPE_CHECKING, Optional, Sequence

import discord
from discord import InteractionResponded

from modules.recruitment import cards
from modules.recruitment import search as roster_search
from modules.recruitment.search_helpers import (
    format_filters_footer as _format_filters_footer,
)
from modules.common import config_access as config
from shared.sheets.recruitment import RecruitmentClanRecord
from modules.recruitment.views.results_pager import ResultsPagerView

if TYPE_CHECKING:
    from cogs.recruitment_recruiter import RecruiterPanelCog

log = logging.getLogger(__name__)

PAGE_SIZE = 10

# Legacy column indices preserved for tests and compatibility shims.
COL_B_CLAN = 1
COL_C_TAG = 2
COL_E_SPOTS = 4

COL_P_CB = 15
COL_Q_HYDRA = 16
COL_R_CHIMERA = 17
COL_S_CVC = 18
COL_T_SIEGE = 19
COL_U_STYLE = 20

IDX_AG_INACTIVES = 32

CB_CHOICES = ["Easy", "Normal", "Hard", "Brutal", "NM", "UNM"]
HYDRA_CHOICES = ["Normal", "Hard", "Brutal", "Nightmare"]
CHIMERA_CHOICES = ["Normal", "Hard", "Brutal", "Nightmare", "UltraNightmare"]
PLAYSTYLE_CHOICES = ["Stress Free", "Casual", "Semi Competitive", "Competitive"]

TOKEN_MAP = {
    "EASY": "ESY",
    "NORMAL": "NML",
    "HARD": "HRD",
    "BRUTAL": "BTL",
    "NM": "NM",
    "UNM": "UNM",
    "ULTRA-NIGHTMARE": "UNM",
    "ULTRANIGHTMARE": "UNM",
}

STYLE_CANON = {
    "STRESS FREE": "STRESSFREE",
    "STRESS-FREE": "STRESSFREE",
    "STRESSFREE": "STRESSFREE",
    "CASUAL": "CASUAL",
    "SEMI COMPETITIVE": "SEMICOMPETITIVE",
    "SEMI-COMPETITIVE": "SEMICOMPETITIVE",
    "SEMICOMPETITIVE": "SEMICOMPETITIVE",
    "COMPETITIVE": "COMPETITIVE",
}


def _norm(value: str) -> str:
    return (value or "").strip().upper()


def _map_token(choice: str) -> str:
    mapped = TOKEN_MAP.get(_norm(choice))
    return mapped if mapped is not None else _norm(choice)


def _cell_has_diff(cell_text: str, token: str | None) -> bool:
    if not token:
        return True
    mapped = _map_token(token)
    cell = _norm(cell_text)
    if mapped in cell:
        return True
    if mapped == "HRD" and "HARD" in cell:
        return True
    if mapped == "NML" and "NORMAL" in cell:
        return True
    if mapped == "BTL" and "BRUTAL" in cell:
        return True
    if mapped == "UNM":
        if "ULTRA NIGHTMARE" in cell:
            return True
        if "ULTRA-NIGHTMARE" in cell:
            return True
        if "ULTRANIGHTMARE" in cell:
            return True
    return False


def _cell_equals_flag(cell_text: str, expected: Optional[str]) -> bool:
    if expected is None:
        return True
    return (cell_text or "").strip() == expected


def _canon_style(value: str) -> Optional[str]:
    if not value:
        return None
    text = value.replace("-", " ")
    text = " ".join(text.split()).upper()
    if text in STYLE_CANON:
        return STYLE_CANON[text]
    if text == "SEMI COMPETITIVE":
        return "SEMICOMPETITIVE"
    if text == "STRESS FREE":
        return "STRESSFREE"
    return text if text in {"STRESSFREE", "CASUAL", "SEMICOMPETITIVE", "COMPETITIVE"} else None


def _split_styles(cell_text: str) -> set[str]:
    import re

    tokens = re.split(r"[,\|/;]+", cell_text or "")
    values: set[str] = set()
    for token in tokens:
        canon = _canon_style(token)
        if canon:
            values.add(canon)
    return values


def _playstyle_ok(cell_text: str, wanted: Optional[str]) -> bool:
    if not wanted:
        return True
    canon = _canon_style(wanted)
    if not canon:
        return True
    return canon in _split_styles(cell_text)


def _page_embeds(
    page_rows: Sequence[RecruitmentClanRecord | Sequence[str]],
    filters_text: str,
    showing_now: int,
    total_available: int,
    page_index: int,
    page_count: int,
) -> list[discord.Embed]:
    if not page_rows:
        return []

    filters_display = (filters_text or "").strip() or "None"
    footer_payload = (
        f"{filters_display} • {showing_now}/{total_available} • Page {page_index + 1}/{page_count}"
    )

    return [
        cards.make_embed_for_row_classic(
            row,
            footer_payload,
            include_crest=False,
        )
        for row in page_rows
    ]


class RecruiterPanelView(discord.ui.View):
    """Interactive filter panel for recruiter searches."""

    DEFAULT_STATUS = "Pick filters and press **Search Clans** to fetch clans."

    def __init__(self, cog: "RecruiterPanelCog", author_id: int) -> None:
        super().__init__(timeout=1800)
        self.cog = cog
        self.author_id = author_id
        self.message: Optional[discord.Message] = None
        self.results_message: Optional[discord.Message] = None
        self.results_view: ResultsPagerView | None = None
        self._update_task: asyncio.Task | None = None
        self._last_panel_embeds: list[discord.Embed] = []
        self._last_results_embeds: list[discord.Embed] = []
        self._last_results_content: str | None = None
        self._last_results_had_pager: bool = False
        self._busy: bool = False
        self.has_searched: bool = False

        self.cb: Optional[str] = None
        self.hydra: Optional[str] = None
        self.chimera: Optional[str] = None
        self.playstyle: Optional[str] = None
        self.cvc: Optional[str] = None
        self.siege: Optional[str] = None
        self.roster_mode: Optional[str] = "open"

        self.matches: list[RecruitmentClanRecord] = []
        self.results_filters_text: str = ""
        self.results_note: str | None = None
        self.total_found: int = 0
        self.page: int = 0
        self.results_stale: bool = False
        self.status_message: str = self.DEFAULT_STATUS

        self._build_components()
        self._reset_filters()

    def _cancel_inflight(self) -> None:
        task = getattr(self, "_update_task", None)
        if task and not task.done():
            task.cancel()
        self._update_task = None

    async def _send_busy_response(self, itx: discord.Interaction) -> None:
        if not itx.response.is_done():
            with contextlib.suppress(Exception):
                await itx.response.defer()

    async def _ack_interaction(self, itx: discord.Interaction) -> None:
        if not itx.response.is_done():
            with contextlib.suppress(Exception):
                await itx.response.defer()

    async def _begin_interaction(self, itx: discord.Interaction) -> bool:
        if self._busy:
            await self._send_busy_response(itx)
            return False
        self._busy = True
        await self._ack_interaction(itx)
        self._cancel_inflight()
        return True

    async def _rebuild_and_edit(self, itx: discord.Interaction) -> None:
        """Build current page and edit the original panel message in place."""

        current_task = asyncio.current_task()
        previous_panel_embeds: list[discord.Embed] = []
        previous_results_embeds: list[discord.Embed] = []
        previous_results_content: str | None = None
        previous_results_had_pager = self._last_results_had_pager
        if self.message:
            try:
                previous_panel_embeds = [
                    embed.copy() for embed in (self.message.embeds or [])
                ]
            except Exception:
                previous_panel_embeds = []
        if self.results_message:
            previous_results_content = self.results_message.content
            try:
                previous_results_embeds = [
                    embed.copy() for embed in (self.results_message.embeds or [])
                ]
            except Exception:
                previous_results_embeds = []
        self._last_results_content = previous_results_content
        self._last_panel_embeds = [embed.copy() for embed in previous_panel_embeds]
        self._last_results_embeds = [embed.copy() for embed in previous_results_embeds]
        try:
            panel_embeds, results_embeds = await self._build_page()
            self._last_panel_embeds = [embed.copy() for embed in panel_embeds]
            self._last_results_embeds = [embed.copy() for embed in results_embeds]
            self._last_results_had_pager = bool(results_embeds)
            if self.message:
                with contextlib.suppress(Exception):
                    await self.message.edit(embeds=panel_embeds or None, view=self)
            if self.has_searched or self.results_message:
                await self._publish_results(results_embeds)
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("failed to rebuild recruiter panel")
            self.status_message = "⚠️ Couldn’t update the panel. Try again."
            self._sync_visuals()
            if self.message:
                with contextlib.suppress(Exception):
                    await self.message.edit(
                        embeds=previous_panel_embeds or None,
                        view=self,
                    )
            await self._restore_results(previous_results_embeds, previous_results_content)
            self._last_panel_embeds = [embed.copy() for embed in previous_panel_embeds]
            self._last_results_embeds = [embed.copy() for embed in previous_results_embeds]
            self._last_results_content = previous_results_content
            self._last_results_had_pager = previous_results_had_pager
        finally:
            if current_task and self._update_task is current_task:
                self._update_task = None
                self._busy = False

    def _has_any_filter(self) -> bool:
        return any(
            [
                self.cb,
                self.hydra,
                self.chimera,
                self.cvc,
                self.siege,
                self.playstyle,
                self.roster_mode is not None,
            ]
        )

    def _build_components(self) -> None:
        cb_select = discord.ui.Select(
            placeholder="CB Difficulty (optional)",
            min_values=0,
            max_values=1,
            options=[discord.SelectOption(label=label, value=label) for label in CB_CHOICES],
            custom_id="rp_cb",
            row=0,
        )
        cb_select.callback = self._on_cb_select
        self.add_item(cb_select)
        self.cb_select = cb_select  # type: ignore[attr-defined]

        hydra_select = discord.ui.Select(
            placeholder="Hydra Difficulty (optional)",
            min_values=0,
            max_values=1,
            options=[discord.SelectOption(label=label, value=label) for label in HYDRA_CHOICES],
            custom_id="rp_hydra",
            row=1,
        )
        hydra_select.callback = self._on_hydra_select
        self.add_item(hydra_select)
        self.hydra_select = hydra_select  # type: ignore[attr-defined]

        chimera_select = discord.ui.Select(
            placeholder="Chimera Difficulty (optional)",
            min_values=0,
            max_values=1,
            options=[
                discord.SelectOption(label=label, value=label) for label in CHIMERA_CHOICES
            ],
            custom_id="rp_chimera",
            row=2,
        )
        chimera_select.callback = self._on_chimera_select
        self.add_item(chimera_select)
        self.chimera_select = chimera_select  # type: ignore[attr-defined]

        playstyle_select = discord.ui.Select(
            placeholder="Playstyle (optional)",
            min_values=0,
            max_values=1,
            options=[
                discord.SelectOption(label=label, value=label)
                for label in PLAYSTYLE_CHOICES
            ],
            custom_id="rp_style",
            row=3,
        )
        playstyle_select.callback = self._on_playstyle_select
        self.add_item(playstyle_select)
        self.playstyle_select = playstyle_select  # type: ignore[attr-defined]

        cvc_button = discord.ui.Button(
            label="CvC: —",
            style=discord.ButtonStyle.secondary,
            custom_id="rp_cvc",
            row=4,
        )
        cvc_button.callback = self._on_cvc_toggle
        self.add_item(cvc_button)
        self.cvc_button = cvc_button  # type: ignore[attr-defined]

        siege_button = discord.ui.Button(
            label="Siege: —",
            style=discord.ButtonStyle.secondary,
            custom_id="rp_siege",
            row=4,
        )
        siege_button.callback = self._on_siege_toggle
        self.add_item(siege_button)
        self.siege_button = siege_button  # type: ignore[attr-defined]

        roster_button = discord.ui.Button(
            label="Open Spots Only",
            style=discord.ButtonStyle.success,
            custom_id="rp_roster",
            row=4,
        )
        roster_button.callback = self._on_roster_toggle
        self.add_item(roster_button)
        self.roster_button = roster_button  # type: ignore[attr-defined]

        reset_button = discord.ui.Button(
            label="Reset",
            style=discord.ButtonStyle.secondary,
            custom_id="rp_reset",
            row=4,
        )
        reset_button.callback = self._on_reset
        self.add_item(reset_button)

        search_button = discord.ui.Button(
            label="Search Clans",
            style=discord.ButtonStyle.primary,
            custom_id="rp_search",
            row=4,
        )
        search_button.callback = self._on_search
        self.add_item(search_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user and interaction.user.id == self.author_id:
            return True
        try:
            await interaction.response.send_message(
                "⚠️ Not your panel. Type **!clanmatch** to summon your own.",
                ephemeral=True,
            )
        except InteractionResponded:
            try:
                await interaction.followup.send(
                    "⚠️ Not your panel. Type **!clanmatch** to summon your own.",
                    ephemeral=True,
                )
            except Exception:  # pragma: no cover - defensive followup
                pass
        return False

    def _sync_visuals(self) -> None:
        for option in self.cb_select.options:
            option.default = option.value == self.cb
        for option in self.hydra_select.options:
            option.default = option.value == self.hydra
        for option in self.chimera_select.options:
            option.default = option.value == self.chimera
        for option in self.playstyle_select.options:
            option.default = option.value == self.playstyle

        def label_for_toggle(name: str, value: Optional[str]) -> str:
            state = "—" if value is None else ("Yes" if value == "1" else "No")
            return f"{name}: {state}"

        self.cvc_button.label = label_for_toggle("CvC", self.cvc)
        self.cvc_button.style = (
            discord.ButtonStyle.success
            if self.cvc == "1"
            else discord.ButtonStyle.danger
            if self.cvc == "0"
            else discord.ButtonStyle.secondary
        )

        self.siege_button.label = label_for_toggle("Siege", self.siege)
        self.siege_button.style = (
            discord.ButtonStyle.success
            if self.siege == "1"
            else discord.ButtonStyle.danger
            if self.siege == "0"
            else discord.ButtonStyle.secondary
        )

        if self.roster_mode == "open":
            self.roster_button.label = "Open Spots Only"
            self.roster_button.style = discord.ButtonStyle.success
        elif self.roster_mode == "inactives":
            self.roster_button.label = "Inactives Only"
            self.roster_button.style = discord.ButtonStyle.danger
        elif self.roster_mode == "full":
            self.roster_button.label = "Full Only"
            self.roster_button.style = discord.ButtonStyle.primary
        else:
            self.roster_button.label = "Any Roster"
            self.roster_button.style = discord.ButtonStyle.secondary

        self._sync_results_pager()

    def _sync_results_pager(self) -> None:
        if self.results_view:
            self.results_view.sync_state()

    @property
    def owner_id(self) -> int:
        return self.author_id

    @property
    def current_page(self) -> int:
        return self.page

    @property
    def total_pages(self) -> int:
        if not self.matches:
            return 0
        return max(1, math.ceil(len(self.matches) / PAGE_SIZE))

    def _ensure_results_view(self) -> ResultsPagerView:
        if not self.results_view:
            self.results_view = ResultsPagerView(self)
        return self.results_view

    async def _publish_results(self, embeds: list[discord.Embed]) -> None:
        if not self.message:
            return

        channel = getattr(self.message, "channel", None)
        if not isinstance(channel, discord.abc.Messageable):
            return

        if not embeds and not self.has_searched:
            if self.results_message:
                with contextlib.suppress(Exception):
                    await self.results_message.delete()
                self.cog.unregister_results_message(self.author_id)
            self.results_message = None
            self.results_view = None
            self._last_results_embeds = []
            self._last_results_content = None
            self._last_results_had_pager = False
            return

        self._last_results_had_pager = bool(embeds)
        view: ResultsPagerView | None = None
        payload_embeds: list[discord.Embed]

        if embeds:
            view = self._ensure_results_view()
            view.sync_state()
            payload_embeds = embeds
        else:
            payload_embeds = [
                discord.Embed(
                    title="No matching clans found",
                    description="Try relaxing or adjusting your filters, then search again.",
                )
            ]
            self.results_view = None

        if self.results_message and self.results_message.channel.id != channel.id:
            self.results_message = None

        message: discord.Message | None = None
        if self.results_message:
            try:
                await self.results_message.edit(
                    content=None,
                    embeds=payload_embeds,
                    view=view,
                )
            except discord.NotFound:
                self.cog.unregister_results_message(self.author_id)
                self.results_message = None
            else:
                message = self.results_message

        if message is None:
            sent = await channel.send(embeds=payload_embeds, view=view)
            message = sent
            self.results_message = sent

        if view:
            view.bind_to(message)
            self.results_view = view
        else:
            self.results_view = None

        self.cog.register_results_message(
            owner_id=self.author_id,
            channel_id=message.channel.id,
            message_id=message.id,
        )
        self._last_results_content = None

    async def _restore_results(
        self,
        embeds: list[discord.Embed],
        content: str | None,
    ) -> None:
        if not self.results_message:
            return

        if not embeds and content is None:
            with contextlib.suppress(Exception):
                await self.results_message.edit(view=None)
            self.results_view = None
            self._last_results_content = content
            self._last_results_had_pager = False
            return

        view: ResultsPagerView | None = None
        if embeds and self._last_results_had_pager:
            view = self._ensure_results_view()
            view.sync_state()
        elif not embeds:
            self.results_view = None

        try:
            await self.results_message.edit(
                content=content,
                embeds=embeds or None,
                view=view,
            )
        except discord.NotFound:
            self.results_message = None
            self.results_view = None
            self.cog.unregister_results_message(self.author_id)
            self._last_results_content = None
            self._last_results_had_pager = False
        else:
            if view and self.results_message:
                view.bind_to(self.results_message)
            self._last_results_content = content
            self._last_results_had_pager = bool(embeds)

    def _reset_filters(self, *, for_user: bool = False) -> None:
        self.cb = self.hydra = self.chimera = self.playstyle = None
        self.cvc = self.siege = None
        self.roster_mode = "open"
        self.matches = []
        self.results_filters_text = ""
        self.results_note = None
        self.total_found = 0
        self.page = 0
        self.results_stale = False
        self.has_searched = False
        self.status_message = (
            "Filters reset. Pick filters and press **Search Clans** to fetch clans."
            if for_user
            else self.DEFAULT_STATUS
        )
        self._sync_visuals()

    def _mark_filters_changed(self) -> None:
        if self.matches:
            self.results_stale = True
            self.status_message = (
                "Filters changed. Press **Search Clans** to refresh your results."
            )
        else:
            self.status_message = self.DEFAULT_STATUS
        self.page = 0
        self._sync_visuals()

    async def _build_page(self) -> tuple[list[discord.Embed], list[discord.Embed]]:
        description_lines = []
        if self.status_message:
            description_lines.append(self.status_message)
        if self.results_stale and self.matches:
            description_lines.append(
                "⚠️ The clan list reflects your **last** search. Press **Search Clans** to refresh."
            )
        description_lines.append(
            "Pick any filters (*you can leave some blank*) and click **Search Clans**.\n"
            "ℹ️ Choose the most important criteria for your recruit — too many filters might narrow things down to zero.\n"
            "ℹ️ Click **Open Spots Only** to cycle roster filters."
        )
        embed = discord.Embed(
            title="Find a C1C Clan for your recruit",
            description="\n\n".join(description_lines),
        )
        embed.set_footer(text="Only the summoner can use this panel.")

        active_filters = _format_filters_footer(
            self.cb,
            self.hydra,
            self.chimera,
            self.cvc,
            self.siege,
            self.playstyle,
            self.roster_mode,
        )
        if active_filters:
            embed.add_field(name="Active filters", value=active_filters, inline=False)

        if self.results_note:
            embed.add_field(
                name="Results note",
                value=self.results_note,
                inline=False,
            )

        panel_embeds = [embed]
        results_embeds: list[discord.Embed] = []
        if self.matches and self.has_searched:
            total_available = len(self.matches)
            page_count = max(1, math.ceil(total_available / PAGE_SIZE))
            if self.page >= page_count:
                self.page = page_count - 1
            page_index = max(0, self.page)
            start = page_index * PAGE_SIZE
            end = min(total_available, start + PAGE_SIZE)
            showing_now = end - start
            filters_text = self.results_filters_text or active_filters or ""
            page_rows = self.matches[start:end]
            results_embeds = _page_embeds(
                page_rows,
                filters_text,
                showing_now,
                total_available,
                page_index,
                page_count,
            )
        return panel_embeds, results_embeds

    async def _on_cb_select(self, interaction: discord.Interaction) -> None:
        new_value = self.cb_select.values[0] if self.cb_select.values else None
        if not await self._begin_interaction(interaction):
            return
        self.cb = new_value
        self._mark_filters_changed()
        self._update_task = asyncio.create_task(self._rebuild_and_edit(interaction))

    async def _on_hydra_select(self, interaction: discord.Interaction) -> None:
        new_value = self.hydra_select.values[0] if self.hydra_select.values else None
        if not await self._begin_interaction(interaction):
            return
        self.hydra = new_value
        self._mark_filters_changed()
        self._update_task = asyncio.create_task(self._rebuild_and_edit(interaction))

    async def _on_chimera_select(self, interaction: discord.Interaction) -> None:
        new_value = self.chimera_select.values[0] if self.chimera_select.values else None
        if not await self._begin_interaction(interaction):
            return
        self.chimera = new_value
        self._mark_filters_changed()
        self._update_task = asyncio.create_task(self._rebuild_and_edit(interaction))

    async def _on_playstyle_select(self, interaction: discord.Interaction) -> None:
        new_value = self.playstyle_select.values[0] if self.playstyle_select.values else None
        if not await self._begin_interaction(interaction):
            return
        self.playstyle = new_value
        self._mark_filters_changed()
        self._update_task = asyncio.create_task(self._rebuild_and_edit(interaction))

    @staticmethod
    def _cycle_toggle(current: Optional[str]) -> Optional[str]:
        if current is None:
            return "1"
        if current == "1":
            return "0"
        return None

    async def _on_cvc_toggle(self, interaction: discord.Interaction) -> None:
        new_value = self._cycle_toggle(self.cvc)
        if not await self._begin_interaction(interaction):
            return
        self.cvc = new_value
        self._mark_filters_changed()
        self._update_task = asyncio.create_task(self._rebuild_and_edit(interaction))

    async def _on_siege_toggle(self, interaction: discord.Interaction) -> None:
        new_value = self._cycle_toggle(self.siege)
        if not await self._begin_interaction(interaction):
            return
        self.siege = new_value
        self._mark_filters_changed()
        self._update_task = asyncio.create_task(self._rebuild_and_edit(interaction))

    async def _on_roster_toggle(self, interaction: discord.Interaction) -> None:
        next_mode: Optional[str]
        if self.roster_mode == "open":
            next_mode = "inactives"
        elif self.roster_mode == "inactives":
            next_mode = "full"
        elif self.roster_mode == "full":
            next_mode = None
        else:
            next_mode = "open"
        if not await self._begin_interaction(interaction):
            return
        self.roster_mode = next_mode
        self._mark_filters_changed()
        self._update_task = asyncio.create_task(self._rebuild_and_edit(interaction))

    async def _on_reset(self, interaction: discord.Interaction) -> None:
        if not await self._begin_interaction(interaction):
            return
        self._reset_filters(for_user=True)
        self._update_task = asyncio.create_task(self._rebuild_and_edit(interaction))

    async def _on_search(self, interaction: discord.Interaction) -> None:
        if not await self._begin_interaction(interaction):
            return
        if not self._has_any_filter():
            self.status_message = "Pick at least one filter, then try again. 🙂"
            self._sync_visuals()
            self._update_task = asyncio.create_task(self._rebuild_and_edit(interaction))
            return

        self._update_task = asyncio.create_task(self._run_search(interaction))

    async def _run_search(self, interaction: discord.Interaction) -> None:
        current_task = asyncio.current_task()
        try:
            try:
                records = await roster_search.fetch_roster_records(force=False)
            except Exception as exc:  # pragma: no cover - defensive guard
                log.exception("failed to fetch clan records", exc_info=exc)
                if self.matches:
                    self.results_stale = True
                self._sync_visuals()
                self.status_message = (
                    "⚠️ I couldn’t load the clan roster. Try again in a moment."
                )
                await self._rebuild_and_edit(interaction)
                return

            self.has_searched = True

            filtered_records, diagnostics = roster_search.filter_records_with_diagnostics(
                records,
                cb=self.cb,
                hydra=self.hydra,
                chimera=self.chimera,
                cvc=self.cvc,
                siege=self.siege,
                playstyle=self.playstyle,
                roster_mode=self.roster_mode,
            )
            filtered_records = roster_search.enforce_inactives_only(
                filtered_records,
                self.roster_mode,
                context="recruiter_panel:pre_pagination",
            )

            filters_summary = _format_filters_footer(
                self.cb,
                self.hydra,
                self.chimera,
                self.cvc,
                self.siege,
                self.playstyle,
                self.roster_mode,
            )

            if not filtered_records:
                log.debug(
                    "recruiter clanmatch returned no rows",
                    extra={
                        "filters": {
                            "cb": self.cb,
                            "hydra": self.hydra,
                            "chimera": self.chimera,
                            "cvc": self.cvc,
                            "siege": self.siege,
                            "playstyle": self.playstyle,
                            "roster_mode": self.roster_mode,
                        },
                        "diagnostics": diagnostics,
                    },
                )
                self.matches = []
                self.results_filters_text = filters_summary
                self.results_note = None
                self.total_found = 0
                self.results_stale = False
                self.status_message = "No matching clans found. Try relaxing your filters."
                self._sync_visuals()
                await self._rebuild_and_edit(interaction)
                return

            soft_cap = max(PAGE_SIZE, config.get_search_results_soft_cap(25))
            total_found = len(filtered_records)
            limited_rows = filtered_records[:soft_cap]
            shown_count = len(limited_rows)
            cap_note = None
            if total_found > shown_count:
                cap_note = (
                    f"Soft cap active: showing {shown_count} of {total_found} clans."
                )

            self.matches = limited_rows
            self.results_filters_text = filters_summary
            self.results_note = cap_note
            self.total_found = total_found
            self.page = 0
            self.results_stale = False
            per_page = max(1, min(PAGE_SIZE, shown_count))
            if cap_note:
                self.status_message = (
                    f"{cap_note} Use the pager below to review {per_page} at a time."
                )
            else:
                if total_found == 1:
                    message = "Found 1 matching clan."
                else:
                    message = f"Found {total_found} matching clans."
                if shown_count > PAGE_SIZE:
                    message += f" Showing {per_page} per page."
                self.status_message = message
            self._sync_visuals()

            await self._rebuild_and_edit(interaction)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("failed to refresh recruiter search results")
            self.status_message = "⚠️ Couldn’t update the panel. Try again."
            self._sync_visuals()
            if self.message:
                with contextlib.suppress(Exception):
                    await self.message.edit(
                        embeds=self._last_panel_embeds or None, view=self
                    )
            await self._restore_results(
                self._last_results_embeds, self._last_results_content
            )
        finally:
            if current_task and self._update_task is current_task:
                self._update_task = None
                if self._busy:
                    self._busy = False

    async def on_prev_page(self, interaction: discord.Interaction) -> None:
        if not await self._begin_interaction(interaction):
            return
        if not self.matches:
            self._update_task = asyncio.create_task(self._rebuild_and_edit(interaction))
            return
        if self.page > 0:
            self.page -= 1
        self._sync_visuals()
        self._update_task = asyncio.create_task(self._rebuild_and_edit(interaction))

    async def on_next_page(self, interaction: discord.Interaction) -> None:
        if not await self._begin_interaction(interaction):
            return
        if not self.matches:
            self._update_task = asyncio.create_task(self._rebuild_and_edit(interaction))
            return
        max_page = max(0, math.ceil(len(self.matches) / PAGE_SIZE) - 1)
        if self.page < max_page:
            self.page += 1
        self._sync_visuals()
        self._update_task = asyncio.create_task(self._rebuild_and_edit(interaction))

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                expired = discord.Embed(
                    title="Find a C1C Clan",
                    description="⏳ This panel expired. Type **!clanmatch** to open a fresh one.",
                )
                await self.message.edit(embeds=[expired], view=self)
            except Exception:  # pragma: no cover - best effort
                pass
        if self.results_view and self.results_message:
            for child in self.results_view.children:
                child.disabled = True
            with contextlib.suppress(Exception):
                await self.results_message.edit(view=self.results_view)
        if self.message:
            self.cog.unregister_panel(self.message.id)

