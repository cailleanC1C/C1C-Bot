"""Recruiter summary discovery helpers shared by onboarding send/recovery paths."""

from __future__ import annotations

import logging

import discord

log = logging.getLogger("c1c.onboarding.summary_detection")

SUMMARY_MARKERS = ("c1c • recruitment summary", "recruitment summary")


def _message_author_id(message: discord.Message) -> int | None:
    author_id = getattr(getattr(message, "author", None), "id", None)
    try:
        return int(author_id) if author_id is not None else None
    except (TypeError, ValueError):
        return None


def _bot_user_id(bot_user: discord.ClientUser | discord.User | discord.Member | None) -> int | None:
    raw_id = getattr(bot_user, "id", None)
    try:
        return int(raw_id) if raw_id is not None else None
    except (TypeError, ValueError):
        return None


def is_recruitment_summary_message(
    message: discord.Message,
    *,
    bot_user: discord.ClientUser | discord.User | discord.Member | None = None,
) -> bool:
    """Return whether ``message`` looks like this bot's recruiter summary."""

    expected_bot_id = _bot_user_id(bot_user)
    if expected_bot_id is not None and _message_author_id(message) != expected_bot_id:
        return False

    content = str(getattr(message, "content", "") or "").casefold()
    if any(marker in content for marker in SUMMARY_MARKERS):
        return True

    embeds = getattr(message, "embeds", []) or []
    for embed in embeds:
        title = str(getattr(embed, "title", "") or "").casefold()
        description = str(getattr(embed, "description", "") or "").casefold()
        author = getattr(embed, "author", None)
        author_name = str(getattr(author, "name", "") or "").casefold()
        footer = getattr(embed, "footer", None)
        footer_text = str(getattr(footer, "text", "") or "").casefold()
        if any(
            marker in value
            for marker in SUMMARY_MARKERS
            for value in (title, description, author_name, footer_text)
        ):
            return True
    return False


async def find_recruitment_summary_message(
    thread: discord.Thread,
    *,
    bot_user: discord.ClientUser | discord.User | discord.Member | None = None,
    history_limit: int = 50,
    suppress_errors: bool = True,
) -> discord.Message | None:
    """Return an existing recruiter summary message from this bot, if one exists."""

    try:
        async for message in thread.history(limit=history_limit):
            if is_recruitment_summary_message(message, bot_user=bot_user):
                return message
    except Exception:
        if not suppress_errors:
            raise
        log.warning("failed checking thread for recruitment summary", exc_info=True)
    return None


async def has_recruitment_summary(thread: discord.Thread, *, history_limit: int = 50) -> bool:
    return await find_recruitment_summary_message(thread, history_limit=history_limit) is not None
