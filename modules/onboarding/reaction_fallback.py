"""Fallback handler for onboarding reaction triggers."""
from __future__ import annotations

from typing import Any, Optional

import discord
from discord import RawReactionActionEvent
from discord.ext import commands

from c1c_coreops import rbac
from modules.common import feature_flags
from modules.onboarding import logs, thread_membership, thread_scopes
from modules.onboarding.controllers.welcome_controller import (
    extract_target_from_message,
    locate_welcome_message,
    locate_welcome_trigger_message,
)
from modules.onboarding.ui import panels
from modules.onboarding.welcome_flow import start_welcome_dialog
from shared.config import get_ticket_tool_bot_id

# Fallback: 🎫 on the Ticket Tool close-button message
FALLBACK_EMOJI = "👍"
TRIGGER_TOKEN = "[#welcome:ticket]"
PROMO_TRIGGER_MAP = {
    "<!-- trigger:promo.r -->": "promo.r",
    "<!-- trigger:promo.m -->": "promo.m",
    "<!-- trigger:promo.l -->": "promo.l",
}


def normalize_spaces(value: str) -> str:
    return " ".join(value.split())


def _promo_trigger_flow(content: str | None) -> str | None:
    text = content or ""
    for trigger, flow in PROMO_TRIGGER_MAP.items():
        if trigger in text:
            return flow
    return None


def _base_context(
    *,
    member: discord.abc.User | discord.Member | None = None,
    thread: discord.Thread | None = None,
    user_id: int | None = None,
    message_id: int | None = None,
) -> dict[str, Any]:
    context = logs.thread_context(thread)
    context["view"] = "panel"
    context["view_tag"] = panels.WELCOME_PANEL_TAG
    context["custom_id"] = "fallback.emoji"
    context["app_permissions"] = "-"
    context["app_permissions_snapshot"] = "-"
    if thread is not None:
        thread_id = getattr(thread, "id", None)
        if thread_id is not None:
            try:
                context["thread_id"] = int(thread_id)
            except (TypeError, ValueError):
                pass
        parent_id = getattr(thread, "parent_id", None)
        if parent_id is not None:
            try:
                context["parent_channel_id"] = int(parent_id)
            except (TypeError, ValueError):
                pass
    if member is not None:
        context["actor"] = logs.format_actor(member)
        actor_name = logs.format_actor_handle(member)
        if actor_name:
            context["actor_name"] = actor_name
    else:
        context["actor"] = f"<{user_id}>" if user_id else logs.format_actor(None)
    context["emoji"] = FALLBACK_EMOJI
    if message_id is not None:
        try:
            context["message_id"] = int(message_id)
        except (TypeError, ValueError):
            pass
    return context


async def _log_reject(
    reason: str,
    *,
    member: discord.abc.User | discord.Member | None = None,
    thread: discord.Thread | None = None,
    parent_id: int | None = None,
    trigger: str | None = None,
    result: str = "rejected",
    level: str = "warn",
    extra: dict[str, Any] | None = None,
) -> None:
    context = _base_context(member=member, thread=thread)
    if parent_id and "parent" not in context:
        context["parent"] = logs.format_parent(parent_id)
    context["result"] = result
    context["reason"] = reason
    context["trigger"] = trigger or "phrase_match"
    if extra:
        context.update(extra)
    await logs.send_welcome_log(level, **context)


async def _find_panel_message(
    thread: discord.Thread,
    *,
    bot_user_id: int | None,
) -> Optional[discord.Message]:
    return await panels.find_panel_message(thread, bot_user_id=bot_user_id)


class OnboardingReactionFallbackCog(commands.Cog):
    """Listen for onboarding fallback emoji reactions and trigger the dialog."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: RawReactionActionEvent) -> None:
        if str(payload.emoji) != FALLBACK_EMOJI:
            return

        bot_user = getattr(self.bot, "user", None)
        if bot_user and payload.user_id == bot_user.id:
            return

        if payload.guild_id is None:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        member: Optional[discord.Member] = payload.member
        if member is None:
            member = guild.get_member(payload.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(payload.user_id)
            except Exception as exc:
                context = _base_context(user_id=payload.user_id)
                context.update({"result": "member_fetch_failed", "trigger": "member_lookup"})
                await logs.send_welcome_exception("warn", exc, **context)
                return

        if not isinstance(member, discord.Member):
            context = _base_context(user_id=payload.user_id)
            context.update({"result": "member_type", "trigger": "member_lookup"})
            await logs.send_welcome_log("warn", **context)
            return

        if getattr(member, "bot", False):
            context = _base_context(member=member, user_id=payload.user_id)
            context.update({"result": "bot_member", "trigger": "member_lookup"})
            await logs.send_welcome_log("info", **context)
            return

        thread: Optional[discord.Thread] = guild.get_thread(payload.channel_id)
        if thread is None:
            channel = self.bot.get_channel(payload.channel_id)
            if isinstance(channel, discord.Thread):
                thread = channel
            else:
                try:
                    channel = await self.bot.fetch_channel(payload.channel_id)
                except Exception as exc:
                    context = _base_context(member=member, user_id=payload.user_id)
                    context.update({"result": "channel_fetch_failed", "trigger": "channel_lookup"})
                    await logs.send_welcome_exception("warn", exc, **context)
                    return
                if isinstance(channel, discord.Thread):
                    thread = channel
        if thread is None:
            return

        in_welcome_scope = thread_scopes.is_welcome_parent(thread)
        in_promo_scope = thread_scopes.is_promo_parent(thread)

        if not (in_welcome_scope or in_promo_scope):
            await _log_reject(
                "wrong_scope",
                member=member,
                thread=thread,
                parent_id=getattr(thread, "parent_id", None),
                trigger="scope_gate",
                result="wrong_scope",
            )
            return

        if not in_promo_scope and not feature_flags.is_enabled("welcome_dialog"):
            await _log_reject(
                "disabled",
                member=member,
                thread=thread,
                parent_id=getattr(thread, "parent_id", None),
                trigger="feature_disabled",
                result="feature_disabled",
                level="info",
            )
            return

        joined, join_error = await thread_membership.ensure_thread_membership(thread)
        if not joined:
            context = _base_context(member=member, thread=thread, message_id=payload.message_id)
            context.update({"result": "thread_join_failed", "trigger": "thread_join"})
            if join_error is not None:
                await logs.send_welcome_exception("error", join_error, **context)
            else:
                await logs.send_welcome_log("error", **context)
            return

        target_user_id: int | None = None
        target_message_id: int | None = None
        target_extra: dict[str, Any] = {}
        try:
            welcome_message = await locate_welcome_message(thread)
        except Exception as exc:
            lookup_context = _base_context(member=member, thread=thread)
            lookup_context.update({"result": "target_lookup_failed", "trigger": "target_lookup"})
            await logs.send_welcome_exception("warn", exc, **lookup_context)
        else:
            target_user_id, target_message_id = extract_target_from_message(welcome_message)
            if target_user_id is not None:
                target_extra["target_user_id"] = target_user_id
            if target_message_id is not None:
                target_extra["target_message_id"] = target_message_id
        if payload.message_id:
            try:
                target_extra.setdefault("message_id", int(payload.message_id))
            except (TypeError, ValueError):
                pass
        try:
            trigger_message = await locate_welcome_trigger_message(
                thread,
                bot_user_id=getattr(getattr(self.bot, "user", None), "id", None),
                preferred_message=welcome_message,
            )
        except Exception as exc:
            lookup_context = _base_context(member=member, thread=thread)
            lookup_context.update({"result": "trigger_lookup_failed", "trigger": "target_lookup"})
            await logs.send_welcome_exception("warn", exc, **lookup_context)
            return
        if trigger_message is not None and int(getattr(trigger_message, "id", 0) or 0) != int(payload.message_id):
            await _log_reject(
                "wrong_message",
                member=member,
                thread=thread,
                parent_id=getattr(thread, "parent_id", None),
                trigger="target_gate",
                result="wrong_message",
                extra={**target_extra, "target_message_id": getattr(trigger_message, "id", None)},
            )
            return

        ticket_tool_id = get_ticket_tool_bot_id()
        actor_is_privileged = rbac.is_admin_member(member) or rbac.is_recruiter(member)
        actor_is_target = target_user_id is not None and int(member.id) == int(target_user_id)

        if target_user_id is None and not actor_is_privileged:
            await _log_reject(
                "ambiguous_target",
                member=member,
                thread=thread,
                parent_id=getattr(thread, "parent_id", None),
                trigger="role_gate",
                result="ambiguous_target",
                extra=target_extra,
            )
            return

        if not (actor_is_target or actor_is_privileged):
            await _log_reject(
                "role_gate",
                member=member,
                thread=thread,
                parent_id=getattr(thread, "parent_id", None),
                trigger="role_gate",
                result="denied_role",
                extra=target_extra,
            )
            return

        try:
            message = await thread.fetch_message(payload.message_id)
        except Exception as exc:
            context = _base_context(member=member, thread=thread)
            context.update({"result": "message_lookup_failed", "trigger": "message_lookup"})
            await logs.send_welcome_exception("warn", exc, **context)
            return

        content = (getattr(message, "content", "") or "")
        content_lower = normalize_spaces(content.lower())
        author_id = getattr(getattr(message, "author", None), "id", None)
        author_is_ticket_tool = ticket_tool_id is not None and author_id == ticket_tool_id

        if in_promo_scope:
            promo_flow = _promo_trigger_flow(content)
            target_extra["promo_flow"] = promo_flow
            if not author_is_ticket_tool:
                await _log_reject(
                    "not_ticket_tool",
                    member=member,
                    thread=thread,
                    parent_id=getattr(thread, "parent_id", None),
                    result="no_trigger",
                    level="warn",
                    extra=target_extra,
                )
                return
            if promo_flow is None:
                await _log_reject(
                    "no_trigger",
                    member=member,
                    thread=thread,
                    parent_id=getattr(thread, "parent_id", None),
                    result="no_trigger",
                    level="warn",
                    extra={**target_extra, "message_id": payload.message_id},
                )
                return
            trigger = "promo_trigger"
        else:
            phrase_match = "by reacting with" in content_lower
            token_match = TRIGGER_TOKEN in content
            eligible = phrase_match or token_match

            if not eligible:
                await _log_reject(
                    "no_trigger",
                    member=member,
                    thread=thread,
                    parent_id=getattr(thread, "parent_id", None),
                    result="no_trigger",
                    level="warn",
                    extra={**target_extra, "message_id": payload.message_id},
                )
                return

            trigger = "token_match" if token_match and not phrase_match else "phrase_match"

        context = _base_context(member=member, thread=thread, message_id=payload.message_id)
        context.update({"trigger": trigger, "result": "emoji_received"})
        context.update(target_extra)
        await logs.send_welcome_log("info", **context)

        bot_user_id = getattr(getattr(self.bot, "user", None), "id", None)
        existing_panel = await _find_panel_message(thread, bot_user_id=bot_user_id)
        if existing_panel is not None:
            if panels.is_panel_live(existing_panel.id):
                dedup_context = _base_context(member=member, thread=thread)
                dedup_context.update({"trigger": trigger, "result": "deduped"})
                dedup_context.update(target_extra)
                await logs.send_welcome_log("warn", **dedup_context)
                return
            restart_context = _base_context(member=member, thread=thread)
            restart_context.update({"trigger": trigger, "result": "restarted"})
            restart_context.update(target_extra)
            try:
                await existing_panel.delete()
            except Exception as exc:
                await logs.send_welcome_exception("warn", exc, **restart_context)
            panels.mark_panel_inactive_by_message(existing_panel.id)
        else:
            pass

        try:
            await start_welcome_dialog(
                thread,
                member,
                source="emoji",
                bot=self.bot,
            )
        except Exception as exc:
            failure_context = _base_context(member=member, thread=thread)
            failure_context.update({"trigger": trigger, "result": "launch_failed"})
            failure_context.update(target_extra)
            await logs.send_welcome_exception("error", exc, **failure_context)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(OnboardingReactionFallbackCog(bot))
