"""Make Create Next Tournament a visible, single-message setup wizard.

The organizer should always know what has been completed, what is still missing, and
that nothing is created until the final confirmation. Tournament timezone is fixed to
UTC, so the freed fifth modal field is used for the optional motto instead of forcing a
separate theme step.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime

import discord

log = logging.getLogger("c1c.community.live_arena.next_tournament_wizard_ux")
_installed = False

_WIZARD_KEYS = {
    "next_tournament_wizard_intro": set(),
    "next_tournament_wizard_details": {
        "tournament_name",
        "short_name",
        "min_participants",
        "max_participants",
        "tournament_motto",
    },
    "next_tournament_wizard_schedule": {
        "tournament_name",
        "short_name",
        "min_participants",
        "max_participants",
        "tournament_motto",
        "signup_opens",
        "signup_closes",
    },
    "next_tournament_wizard_review": {
        "tournament_name",
        "short_name",
        "min_participants",
        "max_participants",
        "tournament_motto",
        "signup_opens",
        "signup_closes",
        "eligible_clans",
    },
}


def _response_done(interaction) -> bool:
    response = getattr(interaction, "response", None)
    checker = getattr(response, "is_done", None)
    return bool(checker()) if callable(checker) else False


async def _edit_after_defer(interaction, *, embed, view) -> None:
    editor = getattr(interaction, "edit_original_response", None)
    if not callable(editor):
        raise RuntimeError("Discord interaction cannot edit the wizard message")
    await editor(embed=embed, view=view)


async def _send_error(interaction, exc) -> None:
    from modules.community.live_arena import next_tournament

    try:
        if not _response_done(interaction):
            await interaction.response.send_message(
                embed=next_tournament.error_embed(exc), ephemeral=True
            )
        else:
            await interaction.followup.send(
                embed=next_tournament.error_embed(exc), ephemeral=True
            )
    except Exception as send_exc:
        log.exception(
            "Live Arena next-tournament wizard error response failed • error=%s: %s",
            type(send_exc).__name__,
            send_exc,
        )


def _motto_display(draft) -> str:
    from modules.community.live_arena.tournament_motto import _clean_motto

    return _clean_motto(getattr(draft, "tournament_motto", "")) or "—"


def _details_values(draft) -> dict[str, object]:
    return {
        "tournament_name": draft.tournament_name,
        "short_name": draft.short_name,
        "min_participants": draft.min_participants,
        "max_participants": draft.max_participants,
        "tournament_motto": _motto_display(draft),
    }


class TournamentSetupStartView(discord.ui.View):
    """First wizard step; the ephemeral parent already passed organizer auth."""

    def __init__(self, manager):
        super().__init__(timeout=900)
        self.manager = manager

    @discord.ui.button(
        label="Start Tournament Setup",
        style=discord.ButtonStyle.primary,
    )
    async def start(self, interaction, _button):
        user_id = getattr(getattr(interaction, "user", None), "id", "unknown")
        sheet_id = getattr(self.manager, "sheet_id", "unknown")
        try:
            await interaction.response.send_modal(TournamentDetailsModal(self.manager))
        except Exception as exc:
            if _response_done(interaction):
                log.warning(
                    "Live Arena tournament-details modal acknowledged but response handoff raised • user=%s • sheet=%s • error=%s: %s",
                    user_id,
                    sheet_id,
                    type(exc).__name__,
                    exc,
                    exc_info=True,
                )
                return
            log.exception(
                "Live Arena tournament-details modal launch failed • user=%s • sheet=%s • error=%s: %s",
                user_id,
                sheet_id,
                type(exc).__name__,
                exc,
            )
            await _send_error(interaction, exc)

    async def on_error(self, interaction, error, item) -> None:
        log.exception(
            "Live Arena next-tournament start view failed • user=%s • sheet=%s • item=%s • error=%s: %s",
            getattr(getattr(interaction, "user", None), "id", "unknown"),
            getattr(self.manager, "sheet_id", "unknown"),
            getattr(item, "custom_id", None) or getattr(item, "label", "unknown"),
            type(error).__name__,
            error,
            exc_info=(type(error), error, error.__traceback__),
        )


class TournamentDetailsModal(discord.ui.Modal, title="Tournament Details · Step 1 of 4"):
    tournament_name = discord.ui.TextInput(label="Tournament name", max_length=100)
    short_name = discord.ui.TextInput(label="Short name", max_length=32)
    minimum = discord.ui.TextInput(label="Minimum players", default="8", max_length=2)
    maximum = discord.ui.TextInput(label="Maximum players", default="16", max_length=2)
    motto = discord.ui.TextInput(
        label="Motto or tagline (optional)",
        placeholder="No maps. No mercy. Just glorious bad decisions.",
        required=False,
        max_length=160,
    )

    def __init__(self, manager):
        super().__init__(timeout=900)
        self.manager = manager

    async def on_submit(self, interaction):
        from modules.community.live_arena import next_tournament
        from modules.community.live_arena.tournament_motto import _clean_motto

        await interaction.response.defer()
        try:
            name, short, minimum, maximum, _ = next_tournament._validate_basics(
                str(self.tournament_name.value),
                str(self.short_name.value),
                str(self.minimum.value),
                str(self.maximum.value),
                "UTC",
            )
            draft = next_tournament.NextTournamentDraft(
                tournament_name=name,
                short_name=short,
                min_participants=minimum,
                max_participants=maximum,
                timezone="UTC",
                tournament_motto=_clean_motto(str(self.motto.value)),
            )
            templates = await next_tournament._load_next_messages(
                self.manager.sheet_id, {"next_tournament_wizard_details"}
            )
            embed = templates["next_tournament_wizard_details"].embed(
                **_details_values(draft)
            )
            await _edit_after_defer(
                interaction,
                embed=embed,
                view=TournamentSignupWindowView(self.manager, draft),
            )
        except Exception as exc:
            log.exception(
                "Live Arena next-tournament details step failed • sheet=%s • error=%s: %s",
                getattr(self.manager, "sheet_id", "unknown"),
                type(exc).__name__,
                exc,
            )
            await _send_error(interaction, exc)


class TournamentSignupWindowView(discord.ui.View):
    def __init__(self, manager, draft):
        super().__init__(timeout=900)
        self.manager, self.draft = manager, draft

    @discord.ui.button(label="Set Signup Window", style=discord.ButtonStyle.primary)
    async def schedule(self, interaction, _button):
        try:
            await interaction.response.send_modal(
                TournamentSignupWindowModal(self.manager, self.draft)
            )
        except Exception as exc:
            log.exception(
                "Live Arena signup-window modal launch failed • sheet=%s • error=%s: %s",
                getattr(self.manager, "sheet_id", "unknown"),
                type(exc).__name__,
                exc,
            )
            await _send_error(interaction, exc)


class TournamentSignupWindowModal(discord.ui.Modal, title="Signup Window · Step 2 of 4"):
    opens = discord.ui.TextInput(
        label="Signup opens (UTC)",
        placeholder="2026-09-01 18:00",
        max_length=16,
    )
    closes = discord.ui.TextInput(
        label="Signup closes (UTC)",
        placeholder="2026-09-06 18:00",
        max_length=16,
    )

    def __init__(self, manager, draft):
        super().__init__(timeout=900)
        self.manager, self.draft = manager, draft

    async def on_submit(self, interaction):
        from modules.community.live_arena import next_tournament

        await interaction.response.defer()
        try:
            opens = next_tournament._parse_local_datetime(
                str(self.opens.value), "UTC", "signup opening time"
            )
            closes = next_tournament._parse_local_datetime(
                str(self.closes.value), "UTC", "signup closing time"
            )
            if closes <= opens:
                raise next_tournament.RegistrationError(
                    "signup closing time must be after signup opening time"
                )
            if closes <= datetime.now(UTC):
                raise next_tournament.RegistrationError(
                    "signup closing time must be in the future"
                )
            draft = replace(
                self.draft,
                signup_opens_at_utc=next_tournament.utc_iso(opens),
                signup_closes_at_utc=next_tournament.utc_iso(closes),
            )
            service = next_tournament.NextTournamentService(self.manager.sheet_id)
            options = await service.clan_options()
            templates = await next_tournament._load_next_messages(
                self.manager.sheet_id, {"next_tournament_wizard_schedule"}
            )
            embed = templates["next_tournament_wizard_schedule"].embed(
                **_details_values(draft),
                signup_opens=next_tournament.discord_timestamp(
                    draft.signup_opens_at_utc
                ),
                signup_closes=next_tournament.discord_timestamp(
                    draft.signup_closes_at_utc
                ),
            )
            await _edit_after_defer(
                interaction,
                embed=embed,
                view=TournamentEligibilityView(self.manager, draft, options),
            )
        except Exception as exc:
            log.exception(
                "Live Arena next-tournament signup-window step failed • sheet=%s • error=%s: %s",
                getattr(self.manager, "sheet_id", "unknown"),
                type(exc).__name__,
                exc,
            )
            await _send_error(interaction, exc)


class TournamentEligibilityView(discord.ui.View):
    def __init__(self, manager, draft, options):
        super().__init__(timeout=900)
        self.manager, self.draft, self.options = manager, draft, options
        defaults = [item.discord_role_id for item in options if item.active_current]
        self.add_item(
            TournamentClanSelect(
                manager,
                draft,
                options,
                defaults=defaults,
            )
        )


class TournamentClanSelect(discord.ui.Select):
    def __init__(self, manager, draft, options, *, defaults):
        super().__init__(
            placeholder="Select eligible clans · Step 3 of 4",
            min_values=1,
            max_values=len(options),
            options=[
                discord.SelectOption(
                    label=item.label,
                    value=item.discord_role_id,
                    default=item.discord_role_id in defaults,
                )
                for item in options
            ],
        )
        self.manager, self.draft, self.clan_options = manager, draft, options

    async def callback(self, interaction):
        from modules.community.live_arena import next_tournament

        await interaction.response.defer()
        try:
            draft = replace(self.draft, eligible_role_ids=tuple(self.values))
            by_role = {item.discord_role_id: item for item in self.clan_options}
            selected = [by_role[value] for value in self.values]
            templates = await next_tournament._load_next_messages(
                self.manager.sheet_id, {"next_tournament_wizard_review"}
            )
            embed = templates["next_tournament_wizard_review"].embed(
                **_details_values(draft),
                signup_opens=next_tournament.discord_timestamp(
                    draft.signup_opens_at_utc
                ),
                signup_closes=next_tournament.discord_timestamp(
                    draft.signup_closes_at_utc
                ),
                eligible_clans=", ".join(item.label for item in selected),
            )
            await _edit_after_defer(
                interaction,
                embed=embed,
                view=next_tournament.ConfirmCreateNextTournamentView(
                    self.manager, draft
                ),
            )
        except Exception as exc:
            log.exception(
                "Live Arena next-tournament eligibility step failed • sheet=%s • error=%s: %s",
                getattr(self.manager, "sheet_id", "unknown"),
                type(exc).__name__,
                exc,
            )
            await _send_error(interaction, exc)


def install() -> None:
    """Install after motto support and the modal handoff hardening boundary."""
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import next_tournament

    for key, placeholders in _WIZARD_KEYS.items():
        next_tournament._NEXT_MESSAGE_KEYS[key] = set(placeholders)

    # The Create New Tournament callback resolves this symbol at click time. Replace
    # the old start view so the first thing the organizer sees is the full checklist.
    next_tournament.NextTournamentStartView = TournamentSetupStartView

    # Keep the canonical name aligned too, so any direct caller/test opening the basic
    # details modal gets the UTC-fixed five-field form rather than the legacy timezone
    # field or the now-obsolete separate motto/theme step.
    next_tournament.NextTournamentBasicsModal = TournamentDetailsModal
