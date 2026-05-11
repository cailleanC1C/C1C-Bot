"""Clan role management commands for staff/admin role cleanup flows."""

from __future__ import annotations

import logging

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


class ClanRoleManagementCog(commands.Cog):
    @tier("user")
    @help_metadata(function_group="recruitment", section="recruitment", access_tier="staff")
    @commands.group(name="clanrole", invoke_without_command=True)
    async def clanrole(self, ctx: commands.Context) -> None:
        await ctx.reply("Usage: `!clanrole remove @member`", mention_author=False)

    @tier("user")
    @help_metadata(function_group="recruitment", section="recruitment", access_tier="staff")
    @clanrole.command(name="remove", help="Remove a member from a clan role and run Raid/Wandering Souls cleanup.")
    async def clanrole_remove(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        if ctx.guild is None or not isinstance(ctx.author, discord.Member):
            await ctx.reply("⚠️ This command can only be used in a server.", mention_author=False)
            return
        if member is None:
            await ctx.reply("Usage: `!clanrole remove @member`", mention_author=False)
            return
        if not is_authorized_clan_role_manager(ctx.author):
            await ctx.reply("⚠️ You do not have permission to remove clan roles.", mention_author=False)
            return

        clan_roles = get_member_clan_roles(member, config.get_clan_role_ids())
        if not clan_roles:
            await ctx.reply(f"{member.mention} has no configured clan role to remove.", mention_author=False)
            return
        if len(clan_roles) > 1:
            view = ClanRoleRemoveView(self, ctx, member, clan_roles)
            msg = await ctx.reply(f"{ctx.author.mention} choose exactly one clan role to remove from {member.mention}.", view=view, mention_author=False)
            view.message = msg
            return

        await ctx.reply(await self.apply_clan_removal_cleanup(ctx, member, clan_roles[0]), mention_author=False)

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
