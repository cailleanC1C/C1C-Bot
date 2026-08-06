"""Discord integration for the Live Arena registration-only workflow."""

from __future__ import annotations
import logging
from datetime import datetime, timedelta, timezone
import discord
from discord.ext import commands
from c1c_coreops.rbac import is_admin_member
from shared.config import is_guild_allowed
from . import config
from .models import (
    AvailabilitySlot,
    RegistrationError,
    Tournament,
    norm,
    truthy,
    parse_weekday,
    slot_local_datetime,
)
from .repository import LiveArenaRepository
from .rendering import choose_row, configured_embed
from .views import (
    AvailabilityView,
    ConfirmView,
    PersistentPanel,
    RosterView,
    TimezoneModal,
)
from .service import LiveArenaService

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
        self.service = LiveArenaService(self.repository) if self.repository else None

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
        rows = await self.repository.rows("tournaments", ("tournament_id", "status"))
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
            str(row.get("signup_closes_at", row.get("signup_deadline", ""))),
            norm(row.get("eligibility_scope", "selected_clans")),
        )
        return cfg, tournament, row

    async def _organizer(self, member):
        if is_admin_member(member):
            return True
        cfg, _, _ = await self._bundle()
        rows = await self.repository.rows("roles", ("role_type", "discord_role_id"))
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

    async def _message_text(self, key, values, fallback):
        messages = await self.repository.rows("messages")
        row = choose_row(messages, key, self.repository.config.active_tournament_id)
        if not row:
            return fallback
        embed = configured_embed(row, values)
        return embed.description or embed.title or fallback

    async def _report_role_failure(self, guild, member, exc):
        cfg = self.repository.config
        log.error(
            "Live Arena participant role sync failed: %s",
            exc,
            extra={"tournament_id": cfg.active_tournament_id, "member_id": member.id},
        )
        await self.repository.audit(
            "participant_role_sync_failed",
            cfg.active_tournament_id,
            self.bot.user.id,
            "participant",
            member.id,
            {},
            {"role_synced": False},
            str(exc),
        )
        destinations = await self.repository.rows("destinations")
        row = next(
            (
                r
                for r in destinations
                if str(r.get("tournament_id")) == cfg.active_tournament_id
                and norm(r.get("destination_key")) == "organizer_log"
                and truthy(r.get("active", True))
            ),
            None,
        )
        channel = guild.get_channel(int(row["channel_id"])) if row else None
        if channel:
            await channel.send(
                embed=discord.Embed(
                    description=(
                        f"⚠️ Participant role sync failed for <@{member.id}>. The confirmed "
                        "registration was retained; run Refresh Registration."
                    )
                )
            )

    @staticmethod
    def _availability_anchor(raw):
        try:
            value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            value = value.astimezone(timezone.utc)
        except (TypeError, ValueError):
            value = datetime.now(timezone.utc)
        return (value - timedelta(days=value.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

    async def handle_action(self, action, interaction):
        try:
            if not interaction.guild or not is_guild_allowed(interaction.guild.id):
                raise RegistrationError("This action is not available in this server.")
            if action in ORGANIZER:
                if not await self._organizer(interaction.user):
                    raise RegistrationError("Tournament organizer access is required.")
                return await self.organizer_action(action, interaction)
            if action in {"join_tournament", "update_availability"}:
                _, tournament, _ = await self._bundle()
                participants = await self.repository.rows("participants")
                current = self.service.participant_for(
                    participants, tournament.tournament_id, interaction.user.id
                )
                if norm(tournament.status) != "signup_open":
                    raise RegistrationError("Registration is not open.")
                if (
                    action == "join_tournament"
                    and current
                    and norm(current.get("status"))
                    not in {"open", "withdrawn", "confirmed"}
                ):
                    raise RegistrationError(
                        "This registration cannot be changed through self-service; contact a tournament organizer."
                    )
                if (
                    action == "join_tournament"
                    and current
                    and norm(current.get("status")) == "confirmed"
                ):
                    return await self.show_registration(interaction)
                if (
                    action == "join_tournament"
                    and not current
                    and self.service.confirmed_count(
                        participants, tournament.tournament_id
                    )
                    >= tournament.maximum_participants
                ):
                    raise RegistrationError("The tournament is at capacity.")
                if action == "update_availability" and (
                    not current or norm(current.get("status")) != "confirmed"
                ):
                    raise RegistrationError(
                        "Only confirmed participants can update availability."
                    )
                return await interaction.response.send_modal(
                    TimezoneModal(
                        self,
                        "join" if action == "join_tournament" else "update",
                        str(current.get("timezone", "")) if current else "",
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
        from .models import validate_timezone

        try:
            timezone_name = validate_timezone(timezone_name)
            cfg, tournament, _ = await self._bundle()
            raw = await self.repository.rows(
                "availability_slots", ("slot_id", "weekday_utc", "start_time_utc")
            )
            slots = [
                AvailabilitySlot(
                    str(r["slot_id"]),
                    parse_weekday(r["weekday_utc"]),
                    str(r["start_time_utc"]),
                    str(r.get("end_time_utc", "")),
                    truthy(r.get("enabled", r.get("active", True))),
                    int(r.get("sort_order") or 0),
                )
                for r in raw
                if str(r.get("tournament_id", cfg.active_tournament_id))
                in ("", cfg.active_tournament_id)
                and truthy(r.get("enabled", r.get("active", True)))
            ]
            selected = []
            if mode == "update":
                availability = await self.repository.rows("participant_availability")
                selected = [
                    str(r["slot_id"])
                    for r in availability
                    if str(r.get("tournament_id")) == cfg.active_tournament_id
                    and str(r.get("discord_user_id")) == str(interaction.user.id)
                ]
            if not slots:
                raise RegistrationError("No availability windows are configured.")
            await self._reply(
                interaction,
                "Choose local-time windows.",
                view=AvailabilityView(
                    self,
                    mode,
                    timezone_name,
                    slots,
                    selected,
                    anchor_monday=self._availability_anchor(
                        tournament.signup_closes_at
                    ),
                ),
            )
        except RegistrationError as exc:
            await self._reply(interaction, str(exc))

    async def submit_availability(self, interaction, session):
        try:
            _, tournament, _ = await self._bundle()
            result = await self.service.register(
                tournament=tournament,
                user_id=str(interaction.user.id),
                display_name=interaction.user.display_name,
                member_role_ids=[r.id for r in interaction.user.roles],
                timezone_name=session.timezone,
                slot_ids=list(session.selected),
            )
            role_warning = ""
            try:
                await self._set_participant_role(interaction.user, True)
            except Exception as exc:
                role_warning = " Registration succeeded, but the Discord role could not be synced; an organizer has been alerted."
                await self._report_role_failure(
                    interaction.guild, interaction.user, exc
                )
            await self.publish_panels(interaction.guild)
            await self._reply(
                interaction,
                await self._message_text(
                    "signup_confirmed",
                    {
                        "tournament_name": tournament.name,
                        "participant_count": "",
                        "max_participants": tournament.maximum_participants,
                        "tournament_status": tournament.status,
                        "roster_parity_summary": "",
                    },
                    "Registration saved."
                    if result["created"]
                    else "Availability update saved.",
                )
                + role_warning,
            )
        except Exception as exc:
            await self._reply(interaction, str(exc))

    async def _participant_role_id(self):
        cfg, _, _ = await self._bundle()
        roles = await self.repository.rows("roles", ("role_type", "discord_role_id"))
        row = next(
            (
                r
                for r in roles
                if str(r.get("tournament_id")) == cfg.active_tournament_id
                and norm(r.get("role_type")) == "participant"
                and truthy(r.get("active", True))
            ),
            None,
        )
        return int(row["discord_role_id"]) if row else None

    async def _set_participant_role(self, member, present):
        role_id = await self._participant_role_id()
        if not role_id:
            return
        role = member.guild.get_role(role_id)
        if not role:
            raise RegistrationError("The configured participant role is unavailable.")
        has = role in member.roles
        if present and not has:
            await member.add_roles(role, reason="Live Arena registration")
        if not present and has:
            await member.remove_roles(role, reason="Live Arena registration")

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
        availability = (
            await self.repository.rows("participant_availability") if p else []
        )
        raw_slots = await self.repository.rows("availability_slots") if p else []
        selected = {
            str(r.get("slot_id"))
            for r in availability
            if str(r.get("tournament_id")) == cfg.active_tournament_id
            and str(r.get("discord_user_id")) == str(interaction.user.id)
        }
        grouped = {}
        anchor = self._availability_anchor(t.signup_closes_at)
        for row in raw_slots:
            if str(row.get("slot_id")) not in selected:
                continue
            slot = AvailabilitySlot(
                str(row["slot_id"]),
                parse_weekday(row["weekday_utc"]),
                str(row["start_time_utc"]),
                str(row.get("end_time_utc", "")),
            )
            local = slot_local_datetime(
                slot, str(p.get("timezone") or "UTC"), anchor_monday=anchor
            )
            grouped.setdefault(local.strftime("%A"), []).append(local.strftime("%H:%M"))
        saved = (
            "\n".join(f"{day}: {', '.join(times)}" for day, times in grouped.items())
            or "None saved"
        )
        valid = len(selected) >= t.minimum_availability and len(grouped) >= 2
        await self._reply(
            interaction,
            "No registration was found."
            if not p
            else f"**{t.name}**\nStatus: {p.get('status')}\nName: {p.get('display_name_at_signup')}\nClan: {p.get('clan_tag_at_signup')}\nTimezone: {p.get('timezone')}\n\n**Saved availability**\n{saved}\nSelected windows: {len(selected)}\nMinimum validity: {'Valid' if valid else 'Needs attention'}",
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
        await self.service.change_participant_status(
            cfg.active_tournament_id,
            str(interaction.user.id),
            "withdrawn",
            interaction.user.id,
            "self_service_withdrawal",
        )
        await self._set_participant_role(interaction.user, False)
        await self.publish_panels(interaction.guild)
        await self._reply(
            interaction,
            await self._message_text(
                "withdrawal_confirmed",
                {
                    "tournament_name": "",
                    "participant_count": "",
                    "max_participants": "",
                    "tournament_status": "withdrawn",
                    "roster_parity_summary": "",
                },
                "Your registration has been withdrawn.",
            ),
        )

    async def organizer_action(self, action, interaction):
        if action == "refresh_registration":
            await self.reconcile_participant_roles(interaction.guild)
            await self.publish_panels(interaction.guild)
            return await self._reply(interaction, "Registration panels refreshed.")
        if action == "view_roster":
            cfg, _, _ = await self._bundle()
            rows = await self.repository.rows("participants")
            active = [
                r
                for r in rows
                if str(r.get("tournament_id")) == cfg.active_tournament_id
                and norm(r.get("status")) in {"confirmed", "removed", "withdrawn"}
            ]
            body = (
                "\n".join(
                    f"`{r.get('participant_slot')}` <@{r.get('discord_user_id')}> — {r.get('status')}"
                    for r in active
                )
                or "No registrations."
            )
            return await self._reply(
                interaction,
                body,
                title="Live Arena roster",
                view=RosterView(self, active),
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
        rows = await self.repository.rows("tournaments")
        rn = int(row.get("_row_number") or rows.index(row) + 2)
        await self.repository.replace_row("tournaments", rn, {"status": desired})
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
        warning = (
            " Confirmed participant count is odd; an organizer should resolve the unmatched player."
            if desired == "signup_closed"
            and self.service.confirmed_count(
                await self.repository.rows("participants"), cfg.active_tournament_id
            )
            % 2
            else ""
        )
        await self._reply(
            interaction, f"Registration status is now {desired}.{warning}"
        )

    async def organizer_participant(self, interaction, user_id, status):
        if not user_id:
            return await self._reply(interaction, "Choose a participant first.")
        if not await self._organizer(interaction.user):
            return await self._reply(
                interaction, "Tournament organizer access is required."
            )
        cfg, tournament, _ = await self._bundle()
        member = (
            interaction.guild.get_member(int(user_id))
            if str(user_id).isdigit()
            else None
        )
        if status == "confirmed":
            if tournament.status != "signup_open":
                raise RegistrationError(
                    "Participants can only be restored while registration is open."
                )
            if not member:
                raise RegistrationError(
                    "The participant is no longer a member of this server."
                )
            participants = await self.repository.rows("participants")
            if (
                self.service.confirmed_count(participants, cfg.active_tournament_id)
                >= tournament.maximum_participants
            ):
                raise RegistrationError("The tournament is at capacity.")
            clans = await self.repository.rows("eligible_clans")
            self.service.eligible_clan(
                [role.id for role in member.roles], clans, cfg.active_tournament_id
            )
        await self.service.change_participant_status(
            cfg.active_tournament_id,
            user_id,
            status,
            interaction.user.id,
            "organizer_action",
        )
        if member:
            await self._set_participant_role(member, status == "confirmed")
        await self.publish_panels(interaction.guild)
        await self._reply(interaction, f"Participant status changed to {status}.")

    async def reconcile_participant_roles(self, guild):
        cfg, _, _ = await self._bundle()
        rows = await self.repository.rows("participants")
        role_id = await self._participant_role_id()
        role = guild.get_role(role_id) if role_id else None
        if not role:
            return
        wanted = {
            int(r["discord_user_id"])
            for r in rows
            if str(r.get("tournament_id")) == cfg.active_tournament_id
            and norm(r.get("status")) == "confirmed"
            and str(r.get("discord_user_id", "")).isdigit()
        }
        for member in list(role.members):
            if member.id not in wanted:
                try:
                    await member.remove_roles(
                        role, reason="Live Arena role reconciliation"
                    )
                    await self.repository.audit(
                        "participant_role_reconciled",
                        cfg.active_tournament_id,
                        self.bot.user.id,
                        "participant",
                        member.id,
                        {"role": True},
                        {"role": False},
                    )
                except Exception as exc:
                    log.error(
                        "Live Arena role reconciliation failed for %s: %s",
                        member.id,
                        exc,
                    )
        for user_id in wanted:
            member = guild.get_member(user_id)
            if member and role not in member.roles:
                try:
                    await member.add_roles(
                        role, reason="Live Arena role reconciliation"
                    )
                    await self.repository.audit(
                        "participant_role_reconciled",
                        cfg.active_tournament_id,
                        self.bot.user.id,
                        "participant",
                        member.id,
                        {"role": False},
                        {"role": True},
                    )
                except Exception as exc:
                    log.error(
                        "Live Arena role reconciliation failed for %s: %s", user_id, exc
                    )

    async def publish_panels(self, guild):
        cfg, t, _ = await self._bundle()
        destinations = await self.repository.rows(
            "destinations", ("destination_key", "channel_id")
        )
        messages = await self.repository.rows("messages")
        participants = await self.repository.rows("participants")
        states = await self.repository.rows("bot_state", ("state_key", "state_value"))
        count = sum(
            str(p.get("tournament_id")) == cfg.active_tournament_id
            and norm(p.get("status")) == "confirmed"
            for p in participants
        )
        if self.public_view:
            disabled = set()
            if t.status != "signup_open":
                disabled.update({"join_tournament", "update_availability"})
            elif count >= t.maximum_participants:
                disabled.add("join_tournament")
            self.public_view.disable_actions(disabled)
        if self.organizer_view:
            disabled = {
                "open_registration",
                "close_registration",
                "reopen_registration",
            }
            enabled = {
                "draft": "open_registration",
                "signup_open": "close_registration",
                "signup_closed": "reopen_registration",
            }.get(t.status)
            if enabled:
                disabled.remove(enabled)
            self.organizer_view.disable_actions(disabled)
        values = {
            "tournament_name": t.name,
            "signup_closes_at": t.signup_closes_at,
            "signup_deadline": t.signup_closes_at,
            "confirmed_count": count,
            "maximum_participants": t.maximum_participants,
            "minimum_availability": t.minimum_availability,
            "status": t.status,
            "participant_count": count,
            "max_participants": t.maximum_participants,
            "tournament_status": t.status,
            "roster_parity_summary": "Even roster"
            if count % 2 == 0
            else "Odd roster — one player is unmatched",
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
                    and norm(d.get("destination_key")) == destination_type
                    and truthy(d.get("active", True))
                ),
                None,
            )
            if not dest:
                raise RegistrationError(
                    f"Destinations has no active {destination_type} row for the active tournament."
                )
            channel = guild.get_channel(int(dest["channel_id"]))
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
