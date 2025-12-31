"""Sheet-driven reaction roles cog."""

from __future__ import annotations

import logging
import re

import discord
from discord.ext import commands

from c1c_coreops.helpers import help_metadata, tier
from c1c_coreops.rbac import admin_only
from shared.sheets import reaction_roles
from shared.sheets.cache_service import cache

log = logging.getLogger("c1c.community.reaction_roles")


def _normalize_emoji(raw: str) -> str:
    return raw.strip()


class ReactionRolesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._rows_by_key: dict[str, list[reaction_roles.ReactionRoleRow]] = {}
        reaction_roles.register_cache_buckets()

    @staticmethod
    def _parse_emoji(emoji_raw: str) -> tuple[str | None, int | None]:
        text = _normalize_emoji(emoji_raw)
        if not text:
            return None, None
        match = re.match(r"^<a?:[^:]+:(\d+)>$", text)
        if match:
            try:
                emoji_id = int(match.group(1))
            except ValueError:
                return None, None
            return None, emoji_id
        return text, None

    @staticmethod
    def _row_matches_location(
        row: reaction_roles.ReactionRoleRow, message: discord.Message
    ) -> bool:
        if row.channel_id is not None and row.channel_id != message.channel.id:
            return False
        if row.thread_id is not None:
            channel_id = getattr(message.channel, "id", None)
            parent_id = getattr(message.channel, "parent_id", None)
            if row.thread_id not in {channel_id, parent_id}:
                return False
        return True

    @staticmethod
    def _emoji_from_row(
        row: reaction_roles.ReactionRoleRow, guild: discord.Guild
    ) -> str | discord.Emoji | None:
        unicode_emoji, emoji_id = ReactionRolesCog._parse_emoji(row.emoji_raw)
        if emoji_id is not None:
            emoji_obj = guild.get_emoji(emoji_id)
            return emoji_obj
        return unicode_emoji if unicode_emoji else None

    async def _refresh_rows(self) -> dict[str, list[reaction_roles.ReactionRoleRow]]:
        try:
            rows = await cache.get("reaction_roles")
            if rows is None:
                await cache.refresh_now("reaction_roles", actor="reaction_roles")
                rows = await cache.get("reaction_roles")
        except Exception:
            log.exception("reaction roles cache lookup failed")
            rows = None

        if not isinstance(rows, (list, tuple)):
            rows = reaction_roles.cached_reaction_roles()

        grouped: dict[str, list[reaction_roles.ReactionRoleRow]] = {}
        for row in rows or []:
            if not isinstance(row, reaction_roles.ReactionRoleRow):
                continue
            grouped.setdefault(row.key, []).append(row)
        self._rows_by_key = grouped
        return grouped

    async def _rows_for_key(self, key: str) -> list[reaction_roles.ReactionRoleRow]:
        await self._refresh_rows()
        return self._rows_by_key.get(key, [])

    async def _active_rows(self) -> list[reaction_roles.ReactionRoleRow]:
        await self._refresh_rows()
        rows: list[reaction_roles.ReactionRoleRow] = []
        for group in self._rows_by_key.values():
            for row in group:
                if not isinstance(row, reaction_roles.ReactionRoleRow):
                    continue
                if not row.active:
                    continue
                rows.append(row)
        return rows

    async def attach_to_message(self, message: discord.Message, key: str) -> int:
        key_norm = (key or "").strip().lower()
        if not key_norm:
            return 0

        rows = await self._rows_for_key(key_norm)
        if not rows:
            log.info(
                "reaction roles attach skipped: key missing",
                extra={"key": key_norm, "message_id": message.id},
            )
            return 0

        guild = message.guild
        if guild is None:
            return 0

        attached = 0
        for row in rows:
            if not row.active:
                continue
            if not self._row_matches_location(row, message):
                continue
            emoji_obj = self._emoji_from_row(row, guild)
            if emoji_obj is None:
                log.error(
                    "reaction roles emoji missing",
                    extra={
                        "key": row.key,
                        "emoji": row.emoji_raw,
                        "guild": getattr(guild, "id", None),
                    },
                )
                continue
            try:
                await message.add_reaction(emoji_obj)
            except discord.Forbidden:
                log.error(
                    "reaction roles emoji add forbidden",
                    extra={
                        "key": row.key,
                        "emoji": row.emoji_raw,
                        "guild": getattr(guild, "id", None),
                    },
                )
            except discord.HTTPException:
                log.exception(
                    "reaction roles emoji add failed",
                    extra={
                        "key": row.key,
                        "emoji": row.emoji_raw,
                        "guild": getattr(guild, "id", None),
                    },
                )
            else:
                attached += 1

        log.info(
            "🎭 reaction-roles: attached",
            extra={
                "key": key_norm,
                "message_id": getattr(message, "id", None),
                "emojis": attached,
                "guild": getattr(message.guild, "id", None),
            },
        )
        return attached

    async def _handle_reaction(
        self,
        *,
        payload: discord.RawReactionActionEvent,
        grant: bool,
    ) -> None:
        if payload.guild_id is None:
            return
        if payload.user_id == getattr(self.bot.user, "id", None):
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        if grant:
            member = payload.member
            if member is None:
                try:
                    member = await guild.fetch_member(payload.user_id)
                except Exception:
                    return
        else:
            member = guild.get_member(payload.user_id)
            if member is None:
                # Raw reaction remove events usually don't include a member object,
                # so we need to fetch it explicitly to allow unsubscribe to work.
                try:
                    member = await guild.fetch_member(payload.user_id)
                except discord.NotFound:
                    # User left the guild; nothing to revoke.
                    return
                except discord.HTTPException:
                    # Soft-fail on API issues; don't block other handlers.
                    return

        if member is None or member.bot:
            return

        channel = self.bot.get_channel(payload.channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(payload.channel_id)
            except Exception:
                return

        try:
            message = await channel.fetch_message(payload.message_id)  # type: ignore[arg-type]
        except (discord.Forbidden, discord.NotFound):
            return
        except Exception:
            log.exception("reaction roles message fetch failed", extra={"message_id": payload.message_id})
            return

        emoji_text: str | None = None
        emoji_id: int | None = None
        if payload.emoji.is_custom_emoji():
            emoji_id = payload.emoji.id
        else:
            emoji_text = str(payload.emoji)

        active_rows = await self._active_rows()
        matches: list[reaction_roles.ReactionRoleRow] = []
        for row in active_rows:
            unicode_emoji, row_emoji_id = self._parse_emoji(row.emoji_raw)
            if emoji_id is not None:
                if row_emoji_id != emoji_id:
                    continue
            elif unicode_emoji != emoji_text:
                continue
            if not self._row_matches_location(row, message):
                continue
            matches.append(row)

        if not matches:
            return

        applied: list[reaction_roles.ReactionRoleRow] = []
        for row in matches:
            role = guild.get_role(row.role_id)
            if role is None:
                continue
            try:
                if grant:
                    if role not in member.roles:
                        await member.add_roles(role, reason=f"reaction-role: key={row.key}")
                else:
                    if role in member.roles:
                        await member.remove_roles(role, reason=f"reaction-role: key={row.key}")
            except Exception:
                log.exception(
                    "reaction roles role mutation failed",
                    extra={
                        "action": "grant" if grant else "revoke",
                        "key": row.key,
                        "role": row.role_id,
                        "user": member.id,
                    },
                )
                continue

            applied.append(row)

        if not applied:
            return

        action = "grant" if grant else "revoke"
        log.info(
            f"🎭 reaction-roles: {action}",
            extra={
                "keys": sorted({row.key for row in applied}),
                "user": member.id,
                "roles": [row.role_id for row in applied],
                "emoji": emoji_text or emoji_id,
                "message_id": payload.message_id,
            },
        )

    @tier("admin")
    @help_metadata(function_group="operational", section="permissions", access_tier="admin")
    @commands.command(name="reactrole")
    @commands.guild_only()
    @admin_only()
    async def reactrole_cmd(self, ctx: commands.Context, key: str) -> None:  # type: ignore[override]
        target = None
        if ctx.message.reference and isinstance(ctx.message.reference.resolved, discord.Message):
            target = ctx.message.reference.resolved

        if target is None:
            await ctx.send(
                "Please reply to the message you want to attach the reaction role to, then run `!reactrole <key>` again.",
            )
            return

        attached = await self.attach_to_message(target, key)
        if attached <= 0:
            await ctx.send(
                "No reaction roles were attached. Check the key, active rows, and any channel/thread restrictions.",
            )
            return

        await ctx.send(
            f"Reaction roles wired: key=`{key.lower()}`, emojis={attached}. Members can now react to toggle the role.",
        )

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        await self._handle_reaction(payload=payload, grant=True)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        await self._handle_reaction(payload=payload, grant=False)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ReactionRolesCog(bot))
