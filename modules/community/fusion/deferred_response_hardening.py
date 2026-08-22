"""Harden deferred Fusion interaction responses.

Fusion's My Progress button acknowledges with a thinking ephemeral defer before Sheet
I/O. The result of that defer is an original interaction response, so populate that
response through ``edit_original_response`` instead of relying on Discord's deprecated
"first follow-up edits the deferred response" compatibility behaviour.

The response boundary also validates select-option values before sending a view.
Discord rejects an entire component payload when one select contains duplicate option
values. Fusion event IDs are persistence keys as well as select values, so duplicate
values indicate invalid event configuration and must never be sent to Discord.
"""

from __future__ import annotations

import logging

import discord

from modules.community.fusion import opt_in_view

log = logging.getLogger("c1c.community.fusion.deferred_response")
_installed = False
_DUPLICATE_EVENT_MESSAGE = (
    "Fusion progress is temporarily unavailable because the event setup contains "
    "duplicate event IDs. An admin needs to correct the fusion event data."
)


def _error_code(error: BaseException) -> int | None:
    code = getattr(error, "code", None)
    try:
        return int(code) if code is not None else None
    except (TypeError, ValueError):
        return None


def _duplicate_select_option_values(view: discord.ui.View | None) -> tuple[str, ...]:
    """Return duplicate option values found inside any select in ``view``."""

    if view is None:
        return tuple()

    duplicates: set[str] = set()
    for item in getattr(view, "children", ()):
        options = getattr(item, "options", None)
        if not options:
            continue
        seen: set[str] = set()
        for option in options:
            value = str(getattr(option, "value", "") or "")
            if value in seen:
                duplicates.add(value)
            else:
                seen.add(value)
    return tuple(sorted(duplicates))


async def _send_or_edit_ephemeral(
    interaction: discord.Interaction,
    *,
    content: str | None = None,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
) -> None:
    """Send the initial response or populate an already-deferred response."""

    duplicate_values = _duplicate_select_option_values(view)
    if duplicate_values:
        log.error(
            "fusion response blocked duplicate select option values",
            extra={
                "duplicate_select_option_values": duplicate_values,
                "component_count": len(getattr(view, "children", ())),
            },
        )
        # Never send a component payload Discord will reject. A duplicate Fusion
        # event_id is also unsafe for saved progress/reminder keys, so fail closed
        # with an actionable message until the source event data is corrected.
        content = _DUPLICATE_EVENT_MESSAGE
        embed = None
        view = None

    if not interaction.response.is_done():
        await interaction.response.send_message(
            content=content,
            embed=embed,
            view=view,
            ephemeral=True,
        )
        return

    editor = getattr(interaction, "edit_original_response", None)
    if callable(editor):
        fields: dict[str, object] = {}
        if content is not None:
            fields["content"] = content
        if embed is not None:
            fields["embed"] = embed
        if view is not None:
            fields["view"] = view
        try:
            await editor(**fields)
            return
        except Exception as exc:
            log.error(
                "fusion deferred original response edit failed",
                extra={
                    "discord_error_code": _error_code(exc) or "",
                    "discord_error_text": str(exc),
                    "has_embed": embed is not None,
                    "has_view": view is not None,
                    "component_count": len(getattr(view, "children", ())) if view is not None else 0,
                },
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            raise

    # Defensive fallback for test doubles or non-standard interaction objects. Normal
    # discord.py Interactions always expose edit_original_response().
    await interaction.followup.send(
        content=content,
        embed=embed,
        view=view,
        ephemeral=True,
    )


def install() -> None:
    """Install the deferred-response boundary once for the Fusion extension."""

    global _installed
    if _installed:
        return
    _installed = True
    opt_in_view._send_or_followup_ephemeral = _send_or_edit_ephemeral
