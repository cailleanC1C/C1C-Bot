"""Shared entrypoint for onboarding welcome flows."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import asyncio

import discord
from discord.ext import commands

from c1c_coreops import rbac
from modules.common import feature_flags
from modules.onboarding import logs

from modules.onboarding import thread_scopes
from modules.onboarding.controllers.promo_controller import PromoController
from modules.onboarding.controllers.welcome_controller import (
    WelcomeController,
    extract_target_from_message,
    locate_welcome_message,
)
from modules.onboarding.ui import panels
from modules.onboarding.watcher_welcome import parse_promo_thread_name
from shared.sheets import onboarding as onboarding_sheets
from shared.sheets import onboarding_questions

__all__ = ["resolve_onboarding_flow", "start_welcome_dialog"]

_PROMO_FLOW_MAP = {"R": "promo.r", "M": "promo.m", "L": "promo.l"}


@dataclass(frozen=True, slots=True)
class FlowResolution:
    flow: str | None
    ticket_code: str | None = None
    error: str | None = None


def resolve_onboarding_flow(thread: discord.Thread) -> FlowResolution:
    """Return the onboarding flow for ``thread`` or an error description."""

    if thread_scopes.is_welcome_parent(thread):
        return FlowResolution("welcome")

    if thread_scopes.is_promo_parent(thread):
        parts = parse_promo_thread_name(getattr(thread, "name", None))
        if not parts:
            return FlowResolution(None, error="promo_ticket_parse_failed")

        prefix = (parts.ticket_code or "")[:1].upper()
        flow = parts.promo_flow or _PROMO_FLOW_MAP.get(prefix)
        if not flow:
            return FlowResolution(None, ticket_code=parts.ticket_code, error="promo_ticket_unknown_prefix")

        return FlowResolution(flow, ticket_code=parts.ticket_code)

    return FlowResolution(None, error="scope_or_role")


def _is_promo_flow(flow: str) -> bool:
    return flow.startswith("promo")


async def start_welcome_dialog(
    thread: discord.Thread,
    initiator: discord.abc.User | discord.Member | Any,
    source: str,
    *,
    bot: commands.Bot | None = None,
    panel_message_id: int | None = None,
    panel_message: discord.Message | None = None,
) -> None:
    """Launch the shared welcome dialog flow when all gates pass."""

    resolution = resolve_onboarding_flow(thread)
    flow = resolution.flow or "unknown"
    actor = initiator if initiator is not None else None

    def _context(
        extra: dict[str, Any] | None = None,
        *,
        actor_override: discord.abc.User | discord.Member | None | object = ...,
    ) -> dict[str, Any]:
        resolved_actor = actor if actor_override is ... else cast(
            discord.abc.User | discord.Member | None, actor_override
        )
        payload = logs.thread_context(thread)
        payload["flow"] = flow
        payload["source"] = source
        guild = getattr(thread, "guild", None)
        parent = getattr(thread, "parent", None)
        payload["guild_id"] = getattr(guild, "id", None)
        payload["parent_channel_id"] = getattr(parent, "id", None)
        payload["thread_id"] = getattr(thread, "id", None)
        payload["thread_name"] = getattr(thread, "name", None)
        payload["actor"] = logs.format_actor(resolved_actor)
        payload["actor_id"] = getattr(resolved_actor, "id", None)
        handle = logs.format_actor_handle(resolved_actor)
        if handle:
            payload["actor_name"] = handle
        if extra:
            payload.update(extra)
        return payload

    async def _safe_notify(message: str) -> None:
        try:
            await thread.send(message)
        except Exception:
            return

    context_defaults: dict[str, Any] = {}
    if resolution.ticket_code:
        context_defaults["ticket_code"] = resolution.ticket_code

    target_user_id: int | None = None
    target_message_id: int | None = None
    try:
        welcome_message = await locate_welcome_message(thread)
    except Exception as exc:  # pragma: no cover - defensive network path
        if resolution.flow is not None:
            await logs.send_welcome_exception(
                "warn",
                exc,
                **_context({"result": "target_lookup_failed"}),
            )
    else:
        target_user_id, target_message_id = extract_target_from_message(welcome_message)

    if target_user_id is not None:
        context_defaults["target_user_id"] = target_user_id
    if target_message_id is not None:
        context_defaults["target_message_id"] = target_message_id

    async def _log_promo_failure_to_sheet(reason: str) -> None:
        payload = _context(
            {
                "flow": "promo",
                "result": "failure",
                "reason": reason,
                **context_defaults,
            }
        )
        if resolution.flow:
            payload["promo_flow"] = resolution.flow
        note = {
            "promo_ticket_parse_failed": "promo_ticket_parse_failed before dialog start",
        }.get(reason, str(reason or "promo failure before dialog start"))
        try:
            await asyncio.to_thread(
                onboarding_sheets.update_ticket_finalization_state,
                "promo",
                ticket=resolution.ticket_code,
                thread_id=getattr(thread, "id", None),
                finalization_status="pending_review",
                finalization_note=note,
            )
        except Exception as exc:  # pragma: no cover - sheet/network path
            await logs.send_welcome_exception("error", exc, **payload)

    if resolution.flow is None:
        await logs.send_welcome_log(
            "warn",
            **_context(
                {
                    "result": "scope_gate",
                    "reason": resolution.error or "scope_or_role",
                    **context_defaults,
                }
            ),
        )
        if resolution.error and resolution.error.startswith("promo_ticket"):
            await _log_promo_failure_to_sheet(resolution.error)
            await _safe_notify(
                "⚠️ I couldn't start the promo dialog for this ticket. Please check the promo ticket number and try again."
            )
        return

    anchor_message_id: int | None = None
    if panel_message is not None:
        raw_id = getattr(panel_message, "id", None)
        try:
            anchor_message_id = int(raw_id) if raw_id is not None else None
        except (TypeError, ValueError):
            anchor_message_id = None
    if panel_message_id is not None:
        context_defaults["message_id"] = panel_message_id
        if anchor_message_id is None:
            anchor_message_id = panel_message_id
    elif anchor_message_id is not None:
        context_defaults["message_id"] = anchor_message_id

    if flow == "welcome" and not feature_flags.is_enabled("recruitment_welcome"):
        await logs.send_welcome_log(
            "info",
            **_context(
                {
                    "result": "feature_disabled",
                    "reason": "recruitment_welcome",
                    **context_defaults,
                }
            ),
        )
        return

    if _is_promo_flow(flow) and not feature_flags.is_enabled("promo_enabled"):
        await logs.send_welcome_log(
            "info",
            **_context({"result": "feature_disabled", "reason": "promo_enabled", **context_defaults}),
        )
        await _safe_notify("⚠️ Promo dialogs are currently disabled.")
        return

    if _is_promo_flow(flow) and not feature_flags.is_enabled("promo_dialog"):
        await logs.send_welcome_log(
            "info",
            **_context({"result": "feature_disabled", "reason": "promo_dialog", **context_defaults}),
        )
        await _safe_notify("⚠️ Promo dialogs are currently disabled.")
        return

    if not feature_flags.is_enabled("welcome_dialog"):
        await logs.send_welcome_log(
            "info", **_context({"result": "feature_disabled", **context_defaults})
        )
        return

    if source != "ticket":
        actor_id = getattr(actor, "id", None)
        actor_is_target = (
            actor_id is not None and target_user_id is not None and int(actor_id) == int(target_user_id)
        )
        actor_is_privileged = bool(
            initiator
            and (rbac.is_admin_member(initiator) or rbac.is_recruiter(initiator))
        )

        if target_user_id is None and not actor_is_privileged:
            await logs.send_welcome_log(
                "warn",
                **_context(
                    {"result": "ambiguous_target", **context_defaults},
                    actor_override=initiator if isinstance(initiator, (discord.Member, discord.User)) else None,
                ),
            )
            return

        if not (actor_is_target or actor_is_privileged):
            await logs.send_welcome_log(
                "warn",
                **_context(
                    {"result": "denied_role", "reason": "scope_or_role", **context_defaults},
                    actor_override=initiator if isinstance(initiator, (discord.Member, discord.User)) else None,
                ),
            )
            return

    schema_version: str | None = None
    questions: list[Any] = []
    try:
        questions = onboarding_questions.get_questions(flow)
        schema_version = onboarding_questions.schema_hash(flow)
    except Exception as exc:
        await logs.send_welcome_exception(
            "error",
            exc,
            **_context({"result": "schema_load_failed", **context_defaults}),
        )
        return

    normalized_scope = "promo" if str(flow or "").startswith("promo") else (flow or "welcome")
    await logs.log_onboarding_panel_lifecycle(
        event="start",
        ticket=thread,
        actor=actor,
        channel=getattr(thread, "parent", None),
        questions=len(questions),
        schema_version=schema_version,
        scope=normalized_scope,
    )

    controller_bot = bot or _resolve_bot(thread)
    if controller_bot is None:
        await logs.send_welcome_log(
            "warn",
            **_context({"result": "missing_bot", **context_defaults}),
        )
        return

    if flow == "welcome":
        controller = WelcomeController(controller_bot)
    else:
        controller = PromoController(controller_bot, flow=flow)
    if anchor_message_id is not None:
        try:
            controller._panel_messages[int(thread.id)] = int(anchor_message_id)
            panels.register_panel_message(int(thread.id), int(anchor_message_id))
        except (TypeError, ValueError):
            anchor_message_id = None
    if panel_message is not None and anchor_message_id is not None:
        controller._prefetched_panels[int(thread.id)] = panel_message
    await controller.run(
        thread,
        actor,
        schema_version,
        questions,
        source=source,
    )


def _resolve_bot(thread: discord.Thread) -> commands.Bot | None:
    state = getattr(thread, "_state", None)
    if state is None:
        guild = getattr(thread, "guild", None)
        state = getattr(guild, "_state", None)
    if state is None:
        return None
    client = getattr(state, "_get_client", None)
    if callable(client):
        bot = client()
        if isinstance(bot, commands.Bot):
            return bot
    bot = getattr(state, "client", None)
    if isinstance(bot, commands.Bot):
        return bot
    return None
