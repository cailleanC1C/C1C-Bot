"""Keep the Rules navigation linked to the managed FAQ landing panel."""

from __future__ import annotations

from typing import Any

import discord

from modules.ops import server_rules as base
from modules.ops import server_rules_interactive as interactive


def _merge_failures(summary: base.Summary, failures: dict[str, list[str]]) -> None:
    for key, reasons in failures.items():
        for reason in reasons:
            summary.fail(key, reason)


async def ensure_faq_jump_link(
    bot: discord.Client,
    target: Any,
    summary: base.Summary,
) -> None:
    """Rebuild the Rules navigation with the FAQ landing panel as the final link."""

    checked, _tab, _headers, rows, preflight_summary = await base.preflight(bot)
    if preflight_summary is not None:
        _merge_failures(summary, preflight_summary.failures)
        return
    if checked is None:
        summary.fail("navigation", "Rules destination is unavailable")
        return

    groups, group_errors = base.build_groups(rows)
    rule_nav, rules, faq_nav, nav_errors = interactive._navigation(groups)
    errors = group_errors + nav_errors
    if errors or rule_nav is None or faq_nav is None:
        for key, reason in errors:
            summary.fail(key, reason)
        return

    if not faq_nav.stored_message_id:
        summary.fail(faq_nav.key, "FAQ navigation has no stored message_id")
        return

    bot_id = getattr(getattr(bot, "user", None), "id", None)
    nav_message, reason = await interactive._fetch_group(
        target, rule_nav, bot_id
    )
    if nav_message is None:
        summary.fail(rule_nav.key, reason)
        return

    try:
        await nav_message.edit(
            content=None,
            embeds=interactive._nav_payload(
                target,
                rule_nav,
                [*rules, faq_nav],
            ),
        )
    except Exception:
        summary.fail(
            rule_nav.key,
            "failed to add the FAQ panel jump link to Rules navigation",
        )
