"""Discord integration for the Live Arena registration-only workflow."""

from __future__ import annotations
import logging
from datetime import datetime, timedelta, timezone
import discord
from discord.ext import commands
from c1c_coreops.rbac import is_admin_member
from shared.config import is_guild_allowed
from modules.community.live_arena_tournament import config
from modules.community.live_arena_tournament.models import (
    AvailabilitySlot,
    RegistrationError,
    Tournament,
    norm,
    truthy,
    parse_weekday,
)
from modules.community.live_arena_tournament.repository import LiveArenaRepository
from modules.community.live_arena_tournament.rendering import (
    choose_row,
    configured_embed,
)
from modules.community.live_arena_tournament.views import (
    AvailabilityView,
    ConfirmView,
    PersistentPanel,
    RosterView,
    TimezoneModal,
)
from modules.community.live_arena_tournament.service import LiveArenaService

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
            errors = []
            requirements = {
                "tournaments": (
                    "tournament_id",
                    "tournament_name",
                    "status",
                    "max_participants",
                    "signup_closes_at",
                    "eligibility_scope",
                ),
                "destinations": (
                    "tournament_id",
                    "destination_key",
                    "channel_id",
                    "active",
                ),
                "roles": ("tournament_id", "role_type", "discord_role_id", "active"),
                "availability_slots": (
                    "slot_id",
                    "weekday_utc",
                    "start_time_utc",
                    "end_time_utc",
                    "end_day_offset",
                    "enabled",
                    "sort_order",
                ),
                "participants": (
                    "tournament_id",
                    "participant_slot",
                    "discord_user_id",
                    "status",
                    "timezone",
                    "signed_up_at",
                    "confirmed_at",
                    "withdrawn_at",
                    "display_name_at_signup",
                    "clan_tag_at_signup",
                    "clan_verification_status",
                    "withdrawal_reason",
                ),
                "participant_availability": (
                    "tournament_id",
                    "discord_user_id",
                    "slot_id",
                    "preference",
                    "created_at",
                    "updated_at",
                ),
                "messages": (
                    "message_key",
                    "title_template",
                    "body_template",
                    "embed_color_hex",
                    "active",
                ),
                "message_components": ("action_id", "label", "sort_order", "active"),
                "bot_state": (
                    "tournament_id",
                    "state_key",
                    "state_value",
                    "updated_at",
                ),
                "audit_log": (
                    "event_id",
                    "tournament_id",
                    "event_type",
                    "actor_discord_user_id",
                    "entity_type",
                    "entity_id",
                    "old_value_json",
                    "new_value_json",
                    "created_at",
                    "notes",
                ),
            }
            loaded = {}
            for table, headers in requirements.items():
                try:
                    loaded[table] = await self.repository.rows(table, headers)
                except Exception as exc:
                    errors.append(str(exc))
            if errors:
                raise RegistrationError(
                    "Live Arena workbook schema errors:\n- " + "\n- ".join(errors)
                )
            tid = cfg.active_tournament_id
            tournament = next(
                (
                    r
                    for r in loaded["tournaments"]
                    if str(r.get("tournament_id")) == tid
                    and truthy(r.get("active", True))
                ),
                None,
            )
            if not tournament:
                errors.append(f"Tournament_Config requires an active row for {tid}.")
            for key in ("signup", "organizer_log"):
                if not any(
                    str(r.get("tournament_id")) == tid
                    and norm(r.get("destination_key")) == key
                    and truthy(r.get("active"))
                    and str(r.get("channel_id", "")).strip()
                    for r in loaded["destinations"]
                ):
                    errors.append(f"Destinations requires active {key} row for {tid}.")
            for role_type in ("organizer", "participant"):
                if not any(
                    str(r.get("tournament_id")) == tid
                    and norm(r.get("role_type")) == role_type
                    and truthy(r.get("active"))
                    and str(r.get("discord_role_id", "")).strip()
                    for r in loaded["roles"]
                ):
                    errors.append(
                        f"Tournament_Roles requires active {role_type} row for {tid}."
                    )
            required_messages = {
                "signup_open",
                "signup_closed",
                "signup_confirmed",
                "withdrawal_confirmed",
                "registration_organizer",
            }
            actual_messages = {
                norm(r.get("message_key"))
                for r in loaded["messages"]
                if truthy(r.get("active"))
            }
            for key in sorted(required_messages - actual_messages):
                errors.append(f"Messages requires active {key} row.")
            required_actions = set(PUBLIC + ORGANIZER)
            actual_actions = {
                str(r.get("action_id"))
                for r in loaded["message_components"]
                if truthy(r.get("active"))
            }
            for key in sorted(required_actions - actual_actions):
                errors.append(f"Message_Components requires active {key} action_id.")
            if (
                tournament
                and norm(tournament.get("eligibility_scope")) == "selected_clans"
            ):
                clans = await self.repository.rows(
                    "eligible_clans",
                    ("tournament_id", "clan_tag", "discord_role_id", "active"),
                )
                if not any(
                    str(r.get("tournament_id")) == tid and truthy(r.get("active"))
                    for r in clans
                ):
                    errors.append(
                        f"Eligible_Clans requires an active role row for {tid}."
                    )
            if errors:
                raise RegistrationError(
                    "Live Arena workbook schema errors:\n- " + "\n- ".join(errors)
                )
            components = loaded["message_components"]
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

    async def _reply(
        self, interaction, description="", title="Live Arena", view=None, embed=None
    ):
        embed = embed or discord.Embed(title=title, description=description)
        target = (
            interaction.followup.send
            if interaction.response.is_done()
            else interaction.response.send_message
        )
        await target(embed=embed, view=view, ephemeral=True)

    async def _message_embed(self, key, values):
        messages = await self.repository.rows("messages")
        row = choose_row(messages, key, self.repository.config.active_tournament_id)
        if not row:
            raise RegistrationError(f"Messages requires an active {key} row.")
        return configured_embed(row, values)

    async def _report_role_failure(self, guild, member, exc):
        cfg = self.repository.config
        log.error(
            "Live Arena participant role sync failed: %s",
            exc,
            extra={"tournament_id": cfg.active_tournament_id, "member_id": member.id},
        )
        try:
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
        except Exception:
            log.exception("live_arena_role_failure_audit_failed")
        try:
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
                            f"⚠️ Participant role sync failed for <@{member.id}>. The Sheet "
                            "state was retained; Discord role sync failed. Run Refresh Registration."
                        )
                    )
                )
        except Exception:
            log.exception("live_arena_role_failure_alert_failed")

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
        from modules.community.live_arena_tournament.models import validate_timezone

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
                    int(float(r.get("sort_order") or 0)),
                    int(float(r.get("end_day_offset") or 0)),
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
                "Choose local-time windows."
                + (
                    " Disabled saved selections were removed."
                    if set(selected) - {s.slot_id for s in slots}
                    else ""
                ),
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
                anchor_monday=session.anchor_monday,
            )
            warnings = list(result.get("warnings", ()))
            try:
                await self._set_participant_role(interaction.user, True)
            except Exception as exc:
                warnings.append(
                    "The Discord participant role could not be synced; an organizer has been alerted."
                )
                await self._report_role_failure(
                    interaction.guild, interaction.user, exc
                )
            try:
                await self.publish_panels(interaction.guild)
            except Exception:
                log.exception(
                    "live_arena_panel_refresh_failed",
                    extra={"tournament_id": tournament.tournament_id},
                )
                warnings.append(
                    "Registration panels could not be refreshed; organizers can repair them with Refresh Registration."
                )
            values = {
                "participant": interaction.user.mention,
                "tournament_name": tournament.name,
                "signup_deadline": self._deadline_text(tournament.signup_closes_at),
                "signup_closes_at": self._deadline_text(tournament.signup_closes_at),
            }
            if result["created"]:
                embed = await self._message_embed("signup_confirmed", values)
            else:
                embed = discord.Embed(
                    title="Availability updated",
                    description=f"{interaction.user.mention}, your availability for **{tournament.name}** was saved.",
                )
            if warnings:
                embed.add_field(
                    name="⚠️ Saved with warning", value="\n".join(warnings), inline=False
                )
            await self._reply(interaction, embed=embed)
            return True
        except Exception as exc:
            await self._reply(interaction, str(exc))
            return False

    @staticmethod
    def _deadline_text(raw):
        if not str(raw).strip():
            raise RegistrationError(
                "Tournament_Config.signup_closes_at is required before registration can open."
            )
        try:
            value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError as exc:
            raise RegistrationError(
                "Tournament_Config.signup_closes_at must be an ISO date/time."
            ) from exc
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return f"<t:{int(value.timestamp())}:F>"

    @staticmethod
    def _timestamp(raw):
        try:
            value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return int(value.timestamp())
        except (TypeError, ValueError):
            return 0

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
        enabled_rows = [
            r for r in raw_slots if truthy(r.get("enabled", r.get("active", True)))
        ]
        enabled_ids = {str(r.get("slot_id")) for r in enabled_rows}
        selected &= enabled_ids
        ordered_windows = []
        for row in enabled_rows:
            if str(row.get("slot_id")) not in selected:
                continue
            slot = AvailabilitySlot(
                str(row["slot_id"]),
                parse_weekday(row["weekday_utc"]),
                str(row["start_time_utc"]),
                str(row.get("end_time_utc", "")),
                True,
                int(float(row.get("sort_order") or 0)),
                int(float(row.get("end_day_offset") or 0)),
            )
            from modules.community.live_arena_tournament.models import slot_local_window

            start, end = slot_local_window(
                slot, str(p.get("timezone") or "UTC"), anchor_monday=anchor
            )
            ordered_windows.append((start, end))
        for start, end in sorted(
            ordered_windows, key=lambda pair: (pair[0].weekday(), pair[0].time())
        ):
            grouped.setdefault(start.strftime("%A"), []).append(
                f"{start.strftime('%H:%M')}–{end.strftime('%a %H:%M')}"
            )
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
        cfg, tournament, _ = await self._bundle()
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
            await self._reply(interaction, "No registration was found.")
            return False
        result = await self.service.change_participant_status(
            cfg.active_tournament_id,
            str(interaction.user.id),
            "withdrawn",
            interaction.user.id,
            "self_service_withdrawal",
        )
        warnings = list(result.get("_warnings", ()))
        try:
            await self._set_participant_role(interaction.user, False)
        except Exception as exc:
            warnings.append(
                "Your registration was withdrawn, but the Discord role could not be removed."
            )
            await self._report_role_failure(interaction.guild, interaction.user, exc)
        try:
            await self.publish_panels(interaction.guild)
        except Exception:
            log.exception(
                "live_arena_panel_refresh_failed",
                extra={"tournament_id": cfg.active_tournament_id},
            )
            warnings.append("The registration panels could not be refreshed.")
        embed = await self._message_embed(
            "withdrawal_confirmed",
            {
                "participant": interaction.user.mention,
                "tournament_name": tournament.name,
            },
        )
        if warnings:
            embed.add_field(
                name="⚠️ Withdrawn with warning", value="\n".join(warnings), inline=False
            )
        await self._reply(interaction, embed=embed)
        return True

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
                and str(r.get("discord_user_id", "")).strip()
            ]
            availability = await self.repository.rows("participant_availability")
            counts = {}
            for row in availability:
                if (
                    str(row.get("tournament_id")) == cfg.active_tournament_id
                    and str(row.get("slot_id", "")).strip()
                ):
                    uid = str(row.get("discord_user_id"))
                    counts[uid] = counts.get(uid, 0) + 1
            groups = []
            for status in ("confirmed", "withdrawn", "removed"):
                groups.append(
                    (status, [r for r in active if norm(r.get("status")) == status])
                )
            groups.append(
                (
                    "other occupied statuses",
                    [
                        r
                        for r in active
                        if norm(r.get("status"))
                        not in {"confirmed", "withdrawn", "removed"}
                    ],
                )
            )
            sections = []
            for label, members in groups:
                if members:
                    details = [
                        f"`{r.get('participant_slot')}` <@{r.get('discord_user_id')}> ({r.get('display_name_at_signup') or 'unknown'}) • clan={r.get('clan_tag_at_signup') or '—'} • tz={r.get('timezone') or '—'} • signed=<t:{self._timestamp(r.get('signed_up_at'))}:f> • windows={counts.get(str(r.get('discord_user_id')), 0)}"
                        for r in members
                    ]
                    sections.append(f"**{label.title()}**\n" + "\n".join(details))
            body = "\n\n".join(sections) or "No registrations."
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
        if action in {"open_registration", "reopen_registration"}:
            self._deadline_text(t.signup_closes_at)
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
        log.info(
            "live_arena_registration_state_changed",
            extra={
                "tournament_id": cfg.active_tournament_id,
                "actor_id": interaction.user.id,
                "old_status": t.status,
                "new_status": desired,
                "action": action,
            },
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
        clans = (
            await self.repository.rows("eligible_clans")
            if status == "confirmed"
            else []
        )
        result = await self.service.change_participant_status(
            cfg.active_tournament_id,
            user_id,
            status,
            interaction.user.id,
            "organizer_action",
            tournament=tournament if status == "confirmed" else None,
            member_present=member is not None,
            member_role_ids=[role.id for role in member.roles] if member else [],
            eligible_rows=clans,
        )
        warnings = list(result.get("_warnings", ()))
        if member:
            try:
                await self._set_participant_role(member, status == "confirmed")
            except Exception as exc:
                warnings.append(
                    "The participant status was saved, but their Discord role could not be synced."
                )
                await self._report_role_failure(interaction.guild, member, exc)
        try:
            await self.publish_panels(interaction.guild)
        except Exception:
            log.exception(
                "live_arena_panel_refresh_failed",
                extra={"tournament_id": cfg.active_tournament_id},
            )
            warnings.append("The registration panels could not be refreshed.")
        suffix = "\n\n⚠️ " + "\n".join(warnings) if warnings else ""
        await self._reply(
            interaction, f"Participant status changed to {status}.{suffix}"
        )

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
                    await self._report_role_failure(guild, member, exc)
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
                    await self._report_role_failure(guild, member, exc)

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
            "signup_closes_at": self._deadline_text(t.signup_closes_at),
            "signup_deadline": self._deadline_text(t.signup_closes_at),
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
            if destination_type == "signup":
                embed.add_field(name="Status", value=t.status, inline=True)
                embed.add_field(
                    name="Roster",
                    value=f"{count}/{t.maximum_participants}",
                    inline=True,
                )
                embed.add_field(
                    name="Minimum availability",
                    value=f"{t.minimum_availability} windows across at least 2 local days",
                    inline=False,
                )
                embed.add_field(
                    name="Signup deadline",
                    value=self._deadline_text(t.signup_closes_at),
                    inline=False,
                )
                embed.add_field(
                    name="Roster requirement",
                    value="The final confirmed roster must contain an even number of players.",
                    inline=False,
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
