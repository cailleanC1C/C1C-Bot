"""Clan role management commands for staff/admin role cleanup flows."""

from __future__ import annotations

import logging
import re

import discord
from discord.ext import commands

from c1c_coreops.helpers import help_metadata, tier
from shared import config

log = logging.getLogger(__name__)


def is_authorized_clan_role_manager(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    allowed_role_ids = (
        config.get_admin_role_ids()
        | config.get_staff_role_ids()
        | config.get_lead_role_ids()
        | config.get_recruiter_role_ids()
        | config.get_clan_lead_ids()
    )
    return bool({r.id for r in member.roles} & allowed_role_ids)


def get_member_clan_roles(member: discord.Member, clan_role_ids: set[int]) -> list[discord.Role]:
    return [role for role in member.roles if role.id in clan_role_ids]


class ClanRoleRemoveSelect(discord.ui.Select):
    def __init__(self, parent: "ClanRoleRemoveView", roles: list[discord.Role]) -> None:
        super().__init__(
            placeholder="Select clan role to remove",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label=r.name, value=str(r.id)) for r in roles],
        )
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.parent_view.handle_selection(interaction, int(self.values[0]))


class ClanRoleRemoveView(discord.ui.View):
    def __init__(self, cog: "ClanRoleManagementCog", ctx: commands.Context, target: discord.Member, roles: list[discord.Role]) -> None:
        super().__init__(timeout=60)
        self.cog = cog
        self.ctx = ctx
        self.target = target
        self.message: discord.Message | None = None
        self.add_item(ClanRoleRemoveSelect(self, roles))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user and interaction.user.id == self.ctx.author.id:
            return True
        await interaction.response.send_message("⚠️ Only the original command invoker can choose a role.", ephemeral=True)
        return False

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(content="Timed out — no clan role changes were made.", view=self)
            except Exception:
                log.debug("failed to disable clanrole remove view on timeout", exc_info=True)

    async def handle_selection(self, interaction: discord.Interaction, role_id: int) -> None:
        selected = next((role for role in self.target.roles if role.id == role_id), None)
        if selected is None:
            await interaction.response.send_message("⚠️ Selected role is no longer on that member. No changes made.", ephemeral=True)
            return
        summary = await self.cog.apply_clan_removal_cleanup(self.ctx, self.target, selected)
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content=summary, view=self)
        self.stop()


class ClanRoleTargetSelect(discord.ui.Select):
    def __init__(self, parent: "ClanRoleTargetView", members: list[discord.Member]) -> None:
        options: list[discord.SelectOption] = []
        for member in members[:25]:
            display_label = member.display_name
            username = member.name
            label = display_label if display_label == username else f"{display_label} ({username})"
            options.append(discord.SelectOption(label=label[:100], value=str(member.id)))
        super().__init__(placeholder="Select target member", min_values=1, max_values=1, options=options)
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.parent_view.handle_selection(interaction, int(self.values[0]))


class ClanRoleTargetView(discord.ui.View):
    def __init__(self, cog: "ClanRoleManagementCog", ctx: commands.Context, members: list[discord.Member]) -> None:
        super().__init__(timeout=60)
        self.cog = cog
        self.ctx = ctx
        self.message: discord.Message | None = None
        self.add_item(ClanRoleTargetSelect(self, members))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user and interaction.user.id == self.ctx.author.id:
            return True
        await interaction.response.send_message("⚠️ Only the original command invoker can choose a member.", ephemeral=True)
        return False

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(content="Timed out — no clan role changes were made.", view=self)
            except Exception:
                log.debug("failed to disable clanrole target view on timeout", exc_info=True)

    async def handle_selection(self, interaction: discord.Interaction, member_id: int) -> None:
        member = self.ctx.guild.get_member(member_id) if self.ctx.guild else None
        if member is None:
            await interaction.response.send_message("⚠️ That member is no longer available in this server. No changes made.", ephemeral=True)
            return
        summary, view = await self.cog.build_clanrole_remove_response(self.ctx, member)
        for child in self.children:
            child.disabled = True
        if view is not None:
            if isinstance(view, ClanRoleRemoveView):
                view.message = interaction.message
            await interaction.response.edit_message(content=summary, view=view)
        else:
            await interaction.response.edit_message(content=summary, view=self)
        self.stop()


class ClanRoleManagementCog(commands.Cog):
    @tier("user")
    @help_metadata(function_group="recruitment", section="recruitment", access_tier="staff", usage="!clanrole remove <member query>")
    @commands.group(name="clanrole", invoke_without_command=True, help="Staff clan-role tools. Use remove with a mention, Discord ID, or exact member name to remove clan roles and run Raid/Wandering Souls cleanup; ambiguous matches open an in-channel selection UI.", brief="Staff tool for clan-role removal and related cleanup.")
    async def clanrole(self, ctx: commands.Context) -> None:
        await ctx.reply("Usage: `!clanrole remove <member query>`", mention_author=False)

    @tier("user")
    @help_metadata(function_group="recruitment", section="recruitment", access_tier="staff", usage="!clanrole remove <member query>")
    @clanrole.command(name="remove", help="Staff-only removal by mention, Discord ID, or exact name. Removes clan role assignments and runs Raid/Wandering Souls cleanup; replies in-channel and may open a target picker for ambiguous matches.", brief="Remove clan roles and related Raid/Wandering Souls state.")
    async def clanrole_remove(self, ctx: commands.Context, *, member_query: str | None = None) -> None:
        if ctx.guild is None or not isinstance(ctx.author, discord.Member):
            await ctx.reply("⚠️ This command can only be used in a server.", mention_author=False)
            return
        if not member_query:
            await ctx.reply("Usage: `!clanrole remove <member query>`", mention_author=False)
            return
        if not is_authorized_clan_role_manager(ctx.author):
            await ctx.reply("⚠️ You do not have permission to remove clan roles.", mention_author=False)
            return
        members = await self.resolve_member_query(ctx, member_query)
        if not members:
            await ctx.reply("⚠️ No matching member found. Try mention, ID, or exact name.", mention_author=False)
            return
        if len(members) > 1:
            view = ClanRoleTargetView(self, ctx, members)
            msg = await ctx.reply(f"{ctx.author.mention} multiple members matched `{member_query}`. Choose the target member.", view=view, mention_author=False)
            view.message = msg
            return
        await self.process_clanrole_remove(ctx, members[0])

    async def resolve_member_query(self, ctx: commands.Context, raw_query: str) -> list[discord.Member]:
        query = raw_query.strip()
        mention_match = re.fullmatch(r"<@!?(\d+)>", query)
        id_text = mention_match.group(1) if mention_match else (query if query.isdigit() else None)
        if id_text:
            member_id = int(id_text)
            cached = ctx.guild.get_member(member_id)
            if cached:
                return [cached]
            try:
                fetched = await ctx.guild.fetch_member(member_id)
                return [fetched]
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return []

        normalized = query.casefold()
        exact: list[discord.Member] = []
        starts_with: list[discord.Member] = []
        for member in ctx.guild.members:
            fields = [member.display_name, member.name]
            if getattr(member, "global_name", None):
                fields.append(member.global_name)
            normalized_fields = [field.casefold() for field in fields if field]
            if any(field == normalized for field in normalized_fields):
                exact.append(member)
            elif any(field.startswith(normalized) for field in normalized_fields):
                starts_with.append(member)
        return exact if exact else starts_with

    async def build_clanrole_remove_response(self, ctx: commands.Context, member: discord.Member) -> tuple[str, discord.ui.View | None]:
        clan_roles = get_member_clan_roles(member, config.get_clan_role_ids())
        if not clan_roles:
            return (f"{member.mention} has no configured clan role to remove.", None)
        if len(clan_roles) > 1:
            return (f"{ctx.author.mention} choose exactly one clan role to remove from {member.mention}.", ClanRoleRemoveView(self, ctx, member, clan_roles))

        return (await self.apply_clan_removal_cleanup(ctx, member, clan_roles[0]), None)

    async def process_clanrole_remove(self, ctx: commands.Context, member: discord.Member) -> None:
        content, view = await self.build_clanrole_remove_response(ctx, member)
        msg = await ctx.reply(content, view=view, mention_author=False)
        if isinstance(view, ClanRoleRemoveView):
            view.message = msg

    async def apply_clan_removal_cleanup(self, ctx: commands.Context, member: discord.Member, clan_role: discord.Role) -> str:
        reason = f"Clan removal cleanup requested by {ctx.author}"
        try:
            await member.remove_roles(clan_role, reason=reason)
            log.info("removed clan role", extra={"member_id": member.id, "role_id": clan_role.id, "actor_id": ctx.author.id})
        except (discord.Forbidden, discord.HTTPException):
            log.exception("failed role mutation")
            return f"❌ Failed to remove {clan_role.mention} from {member.mention} due to role hierarchy or permissions."

        success = [f"Removed {clan_role.mention} from {member.mention}."]
        failures: list[str] = []
        remaining_clan_roles = [
            role for role in member.roles if role.id in config.get_clan_role_ids() and role.id != clan_role.id
        ]
        if remaining_clan_roles:
            log.info("skipped Raid because another clan role remains", extra={"member_id": member.id, "actor_id": ctx.author.id})
            return " ".join(success + ["Raid kept because another clan role remains."])

        raid_role_id = config.get_raid_role_id()
        raid_role = member.guild.get_role(raid_role_id or 0)
        if raid_role_id and raid_role is None:
            failures.append("Configured Raid role could not be found")
        elif raid_role and raid_role in member.roles:
            try:
                await member.remove_roles(raid_role, reason=reason)
                success.append("Removed Raid")
                log.info("removed Raid", extra={"member_id": member.id, "actor_id": ctx.author.id})
            except (discord.Forbidden, discord.HTTPException):
                failures.append("Failed to remove Raid")
                log.exception("failed role mutation")
        elif raid_role:
            success.append("Raid was already absent")

        wanderer_role_id = config.get_wandering_souls_role_id()
        wanderer_role = member.guild.get_role(wanderer_role_id or 0)
        if wanderer_role_id and wanderer_role is None:
            failures.append("Configured Wandering Souls role could not be found")
        elif wanderer_role and wanderer_role not in member.roles:
            try:
                await member.add_roles(wanderer_role, reason=reason)
                success.append("Added Wandering Souls")
                log.info("added Wandering Souls", extra={"member_id": member.id, "actor_id": ctx.author.id})
            except (discord.Forbidden, discord.HTTPException):
                failures.append("Failed to add Wandering Souls")
                log.exception("failed role mutation")
        elif wanderer_role:
            success.append("Wandering Souls was already present")

        msg = "; ".join(success) + "."
        if failures:
            msg += f" ⚠️ {'; '.join(failures)}."
        return msg


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ClanRoleManagementCog())
