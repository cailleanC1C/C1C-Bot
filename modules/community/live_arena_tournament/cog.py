"""Discord integration for the Live Arena registration-only workflow."""

from __future__ import annotations
import logging
import discord
from discord.ext import commands
from c1c_coreops.rbac import is_admin_member
from shared.config import is_guild_allowed
from . import config
from .models import RegistrationError, Tournament, norm, truthy
from .repository import LiveArenaRepository
from .rendering import choose_row, configured_embed
from .views import ConfirmView, PersistentPanel, TimezoneModal

log = logging.getLogger("c1c.community.live_arena_registration")
PUBLIC = ("join_tournament", "my_registration", "update_availability", "withdraw")
ORGANIZER = (
    "open_registration",
    "close_registration",
    "reopen_registration",
    "view_roster",
    "refresh_registration",
)


class LiveArenaTournamentCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.repository = (
            LiveArenaRepository(config.sheet_id()) if config.enabled() else None
        )
        self.public_view = None
        self.organizer_view = None

    async def prepare(self):
        if not self.repository:
            log.warning(
                "Live Arena registration disabled: LIVE_ARENA_TOURNAMENT_SHEET_ID is missing"
            )
            return False
        try:
            cfg = await self.repository.load_config()
            components = await self.repository.rows("message_components", ("label",))
            self.public_view = PersistentPanel(self, components, PUBLIC)
            self.organizer_view = PersistentPanel(self, components, ORGANIZER)
            log.info(
                "Live Arena workbook schema loaded",
                extra={"tournament_id": cfg.active_tournament_id},
            )
            return True
        except Exception:
            log.exception(
                "Live Arena registration disabled: actionable workbook/schema load failure"
            )
            self.repository = None
            return False

    async def _bundle(self):
        if not self.repository:
            raise RegistrationError("Live Arena registration is disabled.")
        cfg = self.repository.config or await self.repository.load_config()
        rows = await self.repository.rows(
            "tournament_config", ("tournament_id", "status")
        )
        row = next(
            (
                r
                for r in rows
                if str(r.get("tournament_id")) == cfg.active_tournament_id
            ),
            None,
        )
        if not row:
            raise RegistrationError(
                "The active tournament is missing from Tournament_Config."
            )
        tournament = Tournament(
            cfg.active_tournament_id,
            str(row.get("tournament_name", row.get("name", ""))),
            norm(row["status"]),
            int(row.get("maximum_participants", row.get("max_participants", 0))),
            int(row.get("minimum_availability", 3)),
            str(row.get("signup_deadline", "")),
            norm(row.get("eligibility_scope", "selected_clans")),
        )
        return cfg, tournament, row

    async def _organizer(self, member):
        if is_admin_member(member):
            return True
        cfg, _, _ = await self._bundle()
        rows = await self.repository.rows(
            "tournament_roles", ("role_type", "discord_role_id")
        )
        ids = {
            str(r["discord_role_id"])
            for r in rows
            if str(r.get("tournament_id")) == cfg.active_tournament_id
            and norm(r.get("role_type")) == "organizer"
            and truthy(r.get("active", True))
        }
        return any(str(r.id) in ids for r in member.roles)

    async def _reply(self, interaction, description, title="Live Arena", view=None):
        embed = discord.Embed(title=title, description=description)
        target = (
            interaction.followup.send
            if interaction.response.is_done()
            else interaction.response.send_message
        )
        await target(embed=embed, view=view, ephemeral=True)

    async def handle_action(self, action, interaction):
        try:
            if not interaction.guild or not is_guild_allowed(interaction.guild.id):
                raise RegistrationError("This action is not available in this server.")
            if action in ORGANIZER:
                if not await self._organizer(interaction.user):
                    raise RegistrationError("Tournament organizer access is required.")
                return await self.organizer_action(action, interaction)
            if action in {"join_tournament", "update_availability"}:
                return await interaction.response.send_modal(
                    TimezoneModal(
                        self, "join" if action == "join_tournament" else "update"
                    )
                )
            if action == "my_registration":
                return await self.show_registration(interaction)
            if action == "withdraw":
                return await self._reply(
                    interaction,
                    "Confirm this registration withdrawal.",
                    view=ConfirmView(self.withdraw),
                )
        except Exception as exc:
            await self._reply(interaction, str(exc))

    async def start_availability(self, interaction, mode, timezone_name):
        # Sessions deliberately remain ephemeral and write nothing. The day/select review UI is built from current slots.
        from .models import validate_timezone

        try:
            validate_timezone(timezone_name)
            await self._reply(
                interaction,
                "Your timezone is valid. Choose availability windows in the configured day selectors, then review and submit.",
            )
        except RegistrationError as exc:
            await self._reply(interaction, str(exc))

    async def show_registration(self, interaction):
        cfg, t, _ = await self._bundle()
        rows = await self.repository.rows("participants")
        p = next(
            (
                r
                for r in rows
                if str(r.get("tournament_id")) == cfg.active_tournament_id
                and str(r.get("discord_user_id")) == str(interaction.user.id)
            ),
            None,
        )
        await self._reply(
            interaction,
            "No registration was found."
            if not p
            else f"**{t.name}**\nStatus: {p.get('status')}\nName: {p.get('display_name_at_signup')}\nClan: {p.get('clan_tag_at_signup')}\nTimezone: {p.get('timezone')}",
        )

    async def withdraw(self, interaction):
        cfg, _, _ = await self._bundle()
        rows = await self.repository.rows("participants")
        p = next(
            (
                r
                for r in rows
                if str(r.get("tournament_id")) == cfg.active_tournament_id
                and str(r.get("discord_user_id")) == str(interaction.user.id)
            ),
            None,
        )
        if not p:
            return await self._reply(interaction, "No registration was found.")
        import datetime

        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        rn = int(p.get("_row_number") or rows.index(p) + 2)
        await self.repository.replace_row(
            "participants",
            rn,
            {
                "status": "withdrawn",
                "withdrawn_at": now,
                "withdrawal_reason": "self_service_withdrawal",
            },
        )
        await self.repository.audit(
            "registration_withdrawn",
            cfg.active_tournament_id,
            interaction.user.id,
            "participant",
            interaction.user.id,
            p,
            {"status": "withdrawn"},
        )
        await self._reply(interaction, "Your registration has been withdrawn.")

    async def organizer_action(self, action, interaction):
        if action == "refresh_registration":
            await self.publish_panels(interaction.guild)
            return await self._reply(interaction, "Registration panels refreshed.")
        if action == "view_roster":
            return await self._reply(
                interaction,
                "The current roster was loaded from the registration workbook.",
            )
        desired = {
            "open_registration": "signup_open",
            "close_registration": "signup_closed",
            "reopen_registration": "signup_open",
        }[action]
        cfg, t, row = await self._bundle()
        from .service import LiveArenaService

        if not LiveArenaService.can_transition(t.status, desired):
            raise RegistrationError(
                f"Cannot change registration from {t.status} to {desired}."
            )
        rows = await self.repository.rows("tournament_config")
        rn = int(row.get("_row_number") or rows.index(row) + 2)
        await self.repository.replace_row("tournament_config", rn, {"status": desired})
        event = (
            {
                "signup_open": "registration_opened",
                "signup_closed": "registration_closed",
            }[desired]
            if action != "reopen_registration"
            else "registration_reopened"
        )
        await self.repository.audit(
            event,
            cfg.active_tournament_id,
            interaction.user.id,
            "tournament",
            cfg.active_tournament_id,
            {"status": t.status},
            {"status": desired},
        )
        await self.publish_panels(interaction.guild)
        await self._reply(interaction, f"Registration status is now {desired}.")

    async def publish_panels(self, guild):
        cfg, t, _ = await self._bundle()
        destinations = await self.repository.rows(
            "destinations", ("destination_type", "discord_channel_id")
        )
        messages = await self.repository.rows("messages")
        participants = await self.repository.rows("participants")
        states = await self.repository.rows("bot_state", ("state_key", "state_value"))
        count = sum(
            str(p.get("tournament_id")) == cfg.active_tournament_id
            and norm(p.get("status")) == "confirmed"
            for p in participants
        )
        values = {
            "tournament_name": t.name,
            "signup_deadline": t.signup_deadline,
            "confirmed_count": count,
            "maximum_participants": t.maximum_participants,
            "minimum_availability": t.minimum_availability,
            "status": t.status,
        }
        state_map = {
            norm(x.get("state_key")): x
            for x in states
            if str(x.get("tournament_id", cfg.active_tournament_id))
            in ("", cfg.active_tournament_id)
        }
        for destination_type, state_key, message_key, view in (
            (
                "signup",
                "signup_message_id",
                "signup_open" if t.status == "signup_open" else "signup_closed",
                self.public_view,
            ),
            (
                "organizer_log",
                "registration_organizer_message_id",
                "registration_organizer",
                self.organizer_view,
            ),
        ):
            dest = next(
                (
                    d
                    for d in destinations
                    if str(d.get("tournament_id")) == cfg.active_tournament_id
                    and norm(d.get("destination_type", d.get("purpose")))
                    == destination_type
                    and truthy(d.get("active", True))
                ),
                None,
            )
            if not dest:
                raise RegistrationError(
                    f"Destinations has no active {destination_type} row for the active tournament."
                )
            channel = guild.get_channel(int(dest["discord_channel_id"]))
            if not channel:
                raise RegistrationError(
                    f"Configured {destination_type} destination is unavailable to the bot."
                )
            embed = configured_embed(
                choose_row(messages, message_key, cfg.active_tournament_id), values
            )
            stored = state_map.get(state_key)
            message = None
            if stored and str(stored.get("state_value", "")).isdigit():
                try:
                    message = await channel.fetch_message(int(stored["state_value"]))
                    await message.edit(embed=embed, view=view)
                    log.info(
                        "Live Arena registration panel edited",
                        extra={"panel": destination_type},
                    )
                except discord.NotFound:
                    log.warning(
                        "Live Arena registration panel missing; rebuilding",
                        extra={"panel": destination_type},
                    )
            if not message:
                message = await channel.send(embed=embed, view=view)
                now = (
                    __import__("datetime")
                    .datetime.now(__import__("datetime").timezone.utc)
                    .isoformat()
                )
                if stored:
                    await self.repository.replace_row(
                        "bot_state",
                        int(stored.get("_row_number") or states.index(stored) + 2),
                        {"state_value": str(message.id), "updated_at": now},
                    )
                else:
                    await self.repository.append(
                        "bot_state",
                        {
                            "tournament_id": cfg.active_tournament_id,
                            "state_key": state_key,
                            "state_value": str(message.id),
                            "updated_at": now,
                        },
                    )
                await self.repository.audit(
                    "registration_panel_rebuilt",
                    cfg.active_tournament_id,
                    self.bot.user.id,
                    "message",
                    message.id,
                    {},
                    {"panel": destination_type},
                )
                log.info(
                    "Live Arena registration panel published",
                    extra={"panel": destination_type},
                )

    @commands.group(name="latournament", invoke_without_command=True)
    async def latournament(self, ctx):
        await ctx.send(
            embed=discord.Embed(
                description="Use the configured registration bootstrap action."
            )
        )

    @latournament.command(name="registration")
    async def registration_bootstrap(self, ctx):
        try:
            if not await self._organizer(ctx.author):
                raise RegistrationError("Tournament organizer access is required.")
            await self.publish_panels(ctx.guild)
            await ctx.send(
                embed=discord.Embed(description="Registration panels are ready.")
            )
        except Exception as exc:
            await ctx.send(embed=discord.Embed(description=str(exc)))
