"""Controller for the sheet-driven onboarding welcome flow."""

from __future__ import annotations

import asyncio
import logging
import math
import re
import sys
from typing import Any, Awaitable, Callable, Dict, Iterable, Mapping, Optional, Sequence, cast

import discord
from discord.ext import commands

from modules.onboarding import diag, logs, rules, submit
from modules.onboarding.schema import (
    Question as SheetQuestionRecord,
    get_cached_welcome_questions,
    load_welcome_questions,
    parse_values_list,
)
from modules.onboarding.sessions import Session, ensure_session_for_thread, utc_now
from modules.onboarding.session_store import SessionData, store
from modules.onboarding.ui.card import RollingCard
from modules.onboarding.ui.panel_message_manager import PanelMessageManager
from modules.onboarding.ui.modal_renderer import WelcomeQuestionnaireModal, build_modals
from modules.onboarding.ui.summary_embed import build_summary_embed, _fallback_welcome_embed
from modules.onboarding.ui.select_renderer import build_select_view
from modules.onboarding.ui.summary_retry import RetryWelcomeSummaryView
from modules.onboarding.ui.views import NextStepView
from modules.onboarding.ui import panels
from shared.logfmt import channel_label, user_label
from shared.sheets.onboarding_questions import Question, schema_hash
from shared.config import get_recruiter_role_ids

log = logging.getLogger(__name__)
gate_log = logging.getLogger("c1c.onboarding.gate")
launch_log = logging.getLogger("c1c.onboarding.controller")

async def _delete_captured_answer_message(message: discord.Message | None) -> None:
    """Best-effort cleanup for user answer messages in onboarding threads.

    Used for both normal answers and promo/welcome fallback captures so that
    threads stay clean once the inline wizard has been updated.
    """

    if message is None:
        return
    try:
        if getattr(getattr(message, "author", None), "bot", False):
            return

        delete = getattr(message, "delete", None)
        if delete is None:
            return

        result = delete()
        if asyncio.iscoroutine(result):
            await result
    except Exception:
        log.warning("failed to delete onboarding answer message", exc_info=True)


# --- Sheet-driven validator (no fallbacks/coercion) --------------------------
NUMERIC_HINTS = ("number",)


def _sheet_regex(meta: dict[str, Any]) -> str | None:
    v = (meta.get("validate") or "").strip()
    if v.lower().startswith("regex:"):
        return v.split(":", 1)[1].strip()
    return None


def validate_answer(meta: dict[str, Any], raw: str) -> tuple[bool, str | None, str | None]:
    """
    (ok, cleaned, error)
    - If sheet provides a regex -> enforce it on the raw string.
    - Else -> accept as-is. No numeric/int fallbacks.
    """

    raw = "" if raw is None else str(raw)

    pattern = _sheet_regex(meta)
    if pattern:
        try:
            match = re.fullmatch(pattern, raw)
        except re.error:
            log.warning(
                "welcome: bad regex in sheet",
                extra={"qid": meta.get("qid"), "pattern": pattern},
            )
            return True, raw, None
        if not match:
            message = meta.get("help") or "Input does not match the required format."
            return False, None, message
        return True, raw, None

    return True, raw, None


def _display_name(user: discord.abc.User | discord.Member | None) -> str:
    if user is None:
        return "<unknown>"
    return (
        getattr(user, "display_name", None)
        or getattr(user, "global_name", None)
        or getattr(user, "name", None)
        or "<unknown>"
    )


def _channel_path(channel: discord.abc.GuildChannel | discord.Thread | None) -> str:
    if isinstance(channel, discord.Thread):
        parent = getattr(channel, "parent", None)
        parent_label = f"#{getattr(parent, 'name', 'unknown')}" if parent else "#unknown"
        return f"{parent_label} › {getattr(channel, 'name', 'thread')}"
    if isinstance(channel, discord.abc.GuildChannel):
        return f"#{getattr(channel, 'name', 'channel')}"
    return "#unknown"


def _log_gate(
    interaction: discord.Interaction,
    *,
    allowed: bool,
    reason: str,
    fallback_thread: discord.Thread | None = None,
    flow: str | None = None,
) -> None:
    channel_obj: discord.abc.GuildChannel | discord.Thread | None
    channel_obj = interaction.channel if isinstance(interaction.channel, (discord.Thread, discord.abc.GuildChannel)) else fallback_thread
    emoji = "✅" if allowed else "🔐"
    status = "ok" if allowed else "deny"
    context: dict[str, str] = {}
    flow_value = (flow or "welcome").strip().lower() or "welcome"
    scope = "promo" if str(flow_value).startswith("promo") else "welcome"
    label = "Promo" if scope == "promo" else "Welcome"
    context["scope"] = scope
    context["scope_label"] = f"{label} gate"
    gate_log.info(
        "%s %s — gate=%s • user=%s • channel=%s • reason=%s",
        emoji,
        context["scope_label"],
        status,
        _display_name(getattr(interaction, "user", None)),
        _channel_path(channel_obj),
        reason,
        extra=context,
    )


def _log_followup_fallback(
    interaction: discord.Interaction,
    *,
    action: str,
    error: Exception,
) -> None:
    gate_log.warning(
        "⚠️ Welcome — followup fallback • action=%s • user=%s • channel=%s • why=%s",
        action,
        _display_name(getattr(interaction, "user", None)),
        _channel_path(getattr(interaction, "channel", None)),
        getattr(error, "__class__", type(error)).__name__,
    )


async def _edit_deferred_response(interaction: discord.Interaction, message: str) -> None:
    try:
        await interaction.edit_original_response(content=message)
    except Exception as exc:  # pragma: no cover - defensive fallback
        _log_followup_fallback(interaction, action="edit_original", error=exc)
        followup = getattr(interaction, "followup", None)
        if followup is None:
            log.debug("followup handler missing; skipping deferred notice")
            return
        try:
            await followup.send(message, ephemeral=True)
        except Exception:  # pragma: no cover - final guard
            log.warning("failed to deliver followup message", exc_info=True)

_PANEL_RETRY_DELAYS = (0.5, 1.0, 2.0)

TEXT_TYPES = {"short", "paragraph", "number"}
SELECT_TYPES = {"single-select", "multi-select"}

STATUS_ICON_WAITING = "✍️"
STATUS_ICON_SAVED = "✅"
STATUS_ICON_ERROR = "⚠️"
STATUS_TEXT_WAITING = "Waiting for your reply."
STATUS_TEXT_SAVED = "Saved. Click Next."
STATUS_TEXT_NUMBER = "Invalid format: Use a number like 12.6M (no commas)."
STATUS_TEXT_SELECT = "Invalid format: Pick one option."
STATUS_TEXT_GENERIC = "Invalid format: {hint}"
MAP_CHANGED_NOTE = "↪ Skipped questions updated."


class RollingCardSession:
    """Lightweight rolling-card flow for welcome text/number questions."""

    def __init__(
        self,
        controller: "WelcomeController",
        *,
        thread: discord.abc.Messageable,
        owner: discord.abc.User | discord.Member | None,
        guild: discord.Guild | None,
        questions: Sequence[SheetQuestionRecord],
    ) -> None:
        self.controller = controller
        self.thread = thread
        self.guild = guild or getattr(thread, "guild", None)
        self.card = RollingCard(thread)
        self.owner: discord.abc.User | discord.Member | None = (
            owner or getattr(thread, "owner", None)
        )
        identifier = getattr(self.owner, "id", None)
        if identifier is None:
            identifier = getattr(thread, "owner_id", None)
        try:
            self.owner_id: int | None = int(identifier) if identifier is not None else None
        except (TypeError, ValueError):
            self.owner_id = None
        self.thread_id: int | None = None
        thread_identifier = getattr(thread, "id", None)
        try:
            self.thread_id = int(thread_identifier) if thread_identifier is not None else None
        except (TypeError, ValueError):
            self.thread_id = None
        self._all_questions: list[SheetQuestionRecord] = list(questions)
        self._steps: list[SheetQuestionRecord] = [
            question for question in questions if self._supports(question)
        ]
        self._current_index = 0
        self._current_question: SheetQuestionRecord | None = None
        self._answers: dict[str, Any] = {}
        self._answer_order: list[SheetQuestionRecord] = []
        self._waiting = False
        self._closed = False
        self._visibility: dict[str, dict[str, str]] = {}
        self._status_question: str | None = None
        self._status_state: str = "waiting"
        self._status_hint: str | None = None
        self._note: str | None = None
        self._recompute_visibility()

    def _recompute_visibility(self) -> None:
        try:
            self._visibility = rules.evaluate_visibility(
                self._all_questions, self._answers
            )
        except Exception:
            self._visibility = {}
        else:
            if self.thread_id is not None:
                self.controller._update_thread_visibility(self.thread_id, self._visibility)

    def _visible_state(self, qid: str | None) -> str:
        if not qid:
            return "show"
        return self._visibility.get(qid, {}).get("state", "show")

    def _seek_visible_index(self, start: int, direction: int) -> int | None:
        total = len(self._steps)
        if direction >= 0 and start < 0:
            start = 0
        if direction < 0 and start >= total:
            start = total - 1
        idx = start
        step = 1 if direction >= 0 else -1
        while 0 <= idx < total:
            question = self._steps[idx]
            qid = getattr(question, "qid", None)
            if not qid or self._visible_state(qid) != "skip":
                return idx
            idx += step
        return None

    @staticmethod
    def _supports(question: SheetQuestionRecord) -> bool:
        qtype = (question.qtype or "").lower()
        return qtype in {"bool"} or qtype.startswith(
            ("short", "paragraph", "number", "single-select", "multi-select")
        )

    def _owner_display_name(self) -> str:
        if self.owner is None:
            return "the ticket owner"
        return (
            getattr(self.owner, "display_name", None)
            or getattr(self.owner, "global_name", None)
            or getattr(self.owner, "name", None)
            or "the ticket owner"
        )

    def _is_owner(self, user: discord.abc.User | None) -> bool:
        if user is None:
            return False
        identifier = getattr(user, "id", None)
        try:
            value = int(identifier) if identifier is not None else None
        except (TypeError, ValueError):
            return False
        if self.owner_id is None and value is not None:
            self.owner_id = value
            self.owner = user
        return value is not None and value == self.owner_id

    async def close(self, *, reason: str | None = None) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            message = await self.card.ensure()
        except Exception:
            message = None
        if message is not None:
            try:
                await message.edit(view=None)
            except Exception:
                pass
        self.controller._complete_rolling(self.thread_id)
        captured = getattr(self.controller, "_captured_msgs", None)
        if captured is not None and self.thread_id is not None:
            captured.pop(int(self.thread_id), None)

    async def start(self) -> None:
        if self._closed:
            return
        if not self._steps:
            message = await self.card.ensure()
            await message.edit(content="**Onboarding — Completed**", view=None)
            self._closed = True
            self.controller._complete_rolling(self.thread_id)
            return
        await self._render_step()

    async def _render_step(self) -> None:
        if self._closed:
            return
        total = len(self._steps)
        while self._current_index < total:
            question = self._steps[self._current_index]
            if self._visible_state(question.qid) == "skip":
                self._current_index += 1
                continue
            break
        else:
            await self._finish()
            return
        question = self._steps[self._current_index]
        self._current_question = question

        visibility = self._visibility.get(question.qid, {})
        required = bool(visibility.get("required", question.required))
        state = visibility.get("state") or ("show" if required else "optional")

        has_answer = self._has_answer(question)
        qtype_value = (question.qtype or getattr(question, "type", "") or "").lower()
        if self._status_question != question.qid:
            self._status_question = question.qid
            self._status_hint = None
            self._status_state = "saved" if has_answer else "waiting"
        self._waiting = (qtype_value in TEXT_TYPES) and not has_answer

        view = self._view_for_question(question, required=required, has_answer=has_answer)
        help_text = question.help or ""
        helper_line: str | None = None
        if qtype_value in TEXT_TYPES:
            helper_line = "Just reply in this thread with your answer."
        badge_kind: str | None
        if state == "optional":
            badge_kind = "optional"
        elif state == "show" and required:
            badge_kind = "required"
        else:
            badge_kind = "required" if required else None

        answer_preview = None
        if has_answer:
            answer_preview = self._value_for_summary(self._answers.get(question.qid))

        await self.card.render_question(
            index=self._current_index + 1,
            total=len(self._steps),
            title=question.label,
            help_text=help_text,
            badge_kind=badge_kind,
            status=self._status_payload(question),
            view=view,
            answer_preview=answer_preview,
            note=self._note,
            helper_line=helper_line,
        )
        self._note = None

    def _view_for_question(
        self,
        question: SheetQuestionRecord,
        *,
        required: bool,
        has_answer: bool,
    ) -> discord.ui.View:
        qtype = (question.qtype or "").lower()
        session = self
        waiting_active = self._waiting and self._status_question == question.qid

        class CardView(discord.ui.View):
            def __init__(self) -> None:
                super().__init__(timeout=None)

                if qtype == "bool":

                    async def submit_bool(interaction: discord.Interaction, choice: bool) -> None:
                        await session._handle_bool_answer(interaction, question, choice)

                    yes_button = discord.ui.Button(
                        label="Yes",
                        style=discord.ButtonStyle.success,
                        custom_id=f"welcome.card.bool:{question.qid}:yes",
                        disabled=waiting_active,
                    )

                    async def yes_callback(interaction: discord.Interaction) -> None:
                        await submit_bool(interaction, True)

                    yes_button.callback = yes_callback  # type: ignore[assignment]
                    self.add_item(yes_button)

                    no_button = discord.ui.Button(
                        label="No",
                        style=discord.ButtonStyle.danger,
                        custom_id=f"welcome.card.bool:{question.qid}:no",
                        disabled=waiting_active,
                    )

                    async def no_callback(interaction: discord.Interaction) -> None:
                        await submit_bool(interaction, False)

                    no_button.callback = no_callback  # type: ignore[assignment]
                    self.add_item(no_button)

                elif qtype.startswith("single-select") or qtype.startswith("multi-select"):
                    select_view = session._select_view(question)
                    if select_view is not None:
                        for child in select_view.children:
                            self.add_item(child)

                session._attach_nav_buttons(
                    self,
                    question,
                    required=required,
                    has_answer=has_answer,
                )

        return CardView()

    def _attach_nav_buttons(
        self,
        view: discord.ui.View,
        question: SheetQuestionRecord,
        *,
        required: bool,
        has_answer: bool,
    ) -> None:
        session = self
        # Only block "Next" when we're actively waiting on this required question.
        waiting_active = (
            self._waiting
            and required
            and self._status_question == question.qid
        )
        can_back = self._seek_visible_index(self._current_index - 1, -1) is not None
        can_next = (not waiting_active) and (has_answer or not required)

        back_button = discord.ui.Button(
            label="Back",
            style=discord.ButtonStyle.secondary,
            custom_id=f"welcome.card.nav:{question.qid}:back",
            disabled=not can_back,
        )

        async def back_callback(interaction: discord.Interaction) -> None:
            await session._handle_back(interaction)

        back_button.callback = back_callback  # type: ignore[assignment]
        back_button.row = 1
        view.add_item(back_button)

        next_button = discord.ui.Button(
            label="Next",
            style=discord.ButtonStyle.primary,
            custom_id=f"welcome.card.nav:{question.qid}:next",
            disabled=not can_next,
        )

        async def next_callback(interaction: discord.Interaction) -> None:
            await session._handle_next(interaction)

        next_button.callback = next_callback  # type: ignore[assignment]
        next_button.row = 1
        view.add_item(next_button)

        cancel_button = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.danger,
            custom_id=f"welcome.card.nav:{question.qid}:cancel",
        )

        async def cancel_callback(interaction: discord.Interaction) -> None:
            await session._handle_cancel(interaction)

        cancel_button.callback = cancel_callback  # type: ignore[assignment]
        cancel_button.row = 1
        view.add_item(cancel_button)

    def _select_view(self, question: SheetQuestionRecord) -> discord.ui.View | None:
        options = parse_values_list(question.validate)
        if not options:
            options = [
                part.strip()
                for part in (question.note or "").split(",")
                if part.strip()
            ]
        if not options:
            return None

        qtype = (question.qtype or "").lower()
        max_values = 1
        if qtype.startswith("multi-select"):
            max_values = 3
            parts = [token for token in qtype.split("-") if token]
            for token in reversed(parts):
                if token.isdigit():
                    try:
                        max_values = max(1, int(token))
                    except (TypeError, ValueError):
                        max_values = 3
                    break
        max_values = min(max_values, len(options)) or 1

        session = self

        class SelectPrompt(discord.ui.View):
            def __init__(self) -> None:
                super().__init__(timeout=None)
                self.add_item(self._build_select())

            def _build_select(self) -> discord.ui.Select:
                class AnswerSelect(discord.ui.Select):
                    async def callback(self, interaction: discord.Interaction) -> None:
                        await session._handle_select_answer(
                            interaction,
                            question,
                            list(self.values),
                            multi=max_values > 1,
                        )

                select = AnswerSelect(
                    placeholder="Select…",
                    min_values=1,
                    max_values=max_values,
                    options=[
                        discord.SelectOption(label=option, value=option)
                        for option in options
                    ],
                )
                return select

        return SelectPrompt()

    async def _handle_bool_answer(
        self,
        interaction: discord.Interaction,
        question: SheetQuestionRecord,
        choice: bool,
    ) -> None:
        if not self._is_owner(getattr(interaction, "user", None)):
            try:
                await interaction.response.send_message(
                    "Only the ticket owner can answer.", ephemeral=True
                )
            except Exception:
                pass
            return
        await self._defer_interaction(interaction)
        token = "yes" if choice else "no"
        await self._store_answer(question, token)

    async def _handle_select_answer(
        self,
        interaction: discord.Interaction,
        question: SheetQuestionRecord,
        values: list[str],
        *,
        multi: bool,
    ) -> None:
        if not self._is_owner(getattr(interaction, "user", None)):
            try:
                await interaction.response.send_message(
                    "Only the ticket owner can answer.", ephemeral=True
                )
            except Exception:
                pass
            return
        await self._defer_interaction(interaction)
        payload: Any
        if multi:
            payload = list(values)
        else:
            payload = values[0] if values else ""
        await self._store_answer(question, payload)

    async def _handle_back(self, interaction: discord.Interaction) -> None:
        if not self._is_owner(getattr(interaction, "user", None)):
            try:
                await interaction.response.send_message(
                    "Only the ticket owner can answer.", ephemeral=True
                )
            except Exception:
                pass
            return
        await self._defer_interaction(interaction)
        await self._advance(direction=-1)

    async def _handle_next(self, interaction: discord.Interaction) -> None:
        if not self._is_owner(getattr(interaction, "user", None)):
            try:
                await interaction.response.send_message(
                    "Only the ticket owner can answer.", ephemeral=True
                )
            except Exception:
                pass
            return
        question = self._current_question
        if question is None:
            await self._defer_interaction(interaction)
            return
        waiting_active = self._waiting and self._status_question == question.qid
        visibility = self._visibility.get(question.qid, {})
        required = bool(visibility.get("required"))
        has_answer = self._has_answer(question)
        if waiting_active or (required and not has_answer):
            await self._defer_interaction(interaction)
            return
        await self._defer_interaction(interaction)
        await self._advance(direction=1)

    async def _handle_cancel(self, interaction: discord.Interaction) -> None:
        if not self._is_owner(getattr(interaction, "user", None)):
            try:
                await interaction.response.send_message(
                    "Only the ticket owner can answer.", ephemeral=True
                )
            except Exception:
                pass
            return
        await self._defer_interaction(interaction)
        await self.close(reason="user_cancelled")
        try:
            message = await self.card.ensure()
        except Exception:
            return
        try:
            await message.edit(
                content="Onboarding cancelled. You can press **Open questions** to start again.",
                view=None,
            )
        except Exception:
            pass

    async def _defer_interaction(self, interaction: discord.Interaction) -> None:
        response = getattr(interaction, "response", None)
        if response is None:
            return
        is_done_attr = getattr(response, "is_done", None)
        already_done = False
        if callable(is_done_attr):
            try:
                already_done = bool(is_done_attr())
            except Exception:
                already_done = False
        elif isinstance(is_done_attr, bool):
            already_done = is_done_attr
        if already_done:
            return
        try:
            await response.defer_update()
        except discord.InteractionResponded:
            pass
        except Exception:
            pass

    def _value_tokens(self, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            text = value.strip()
            return [text] if text else []
        if isinstance(value, Mapping):
            tokens: list[str] = []
            label = value.get("label") or value.get("value")
            if isinstance(label, str) and label.strip():
                tokens.append(label.strip())
            nested = value.get("values")
            if isinstance(nested, Iterable) and not isinstance(nested, (str, bytes, bytearray)):
                for item in nested:
                    tokens.extend(self._value_tokens(item))
            return tokens
        if isinstance(value, Iterable) and not isinstance(value, (str, bytes, bytearray)):
            tokens: list[str] = []
            for item in value:
                tokens.extend(self._value_tokens(item))
            return tokens
        text = str(value).strip()
        return [text] if text else []

    def _value_for_summary(self, value: Any) -> str:
        tokens = self._value_tokens(value)
        if tokens:
            return ", ".join(self._format_display_token(token) for token in tokens)
        return str(value)

    def _value_for_store(self, value: Any) -> str:
        tokens = self._value_tokens(value)
        if tokens:
            return ", ".join(tokens)
        return str(value)

    @staticmethod
    def _format_display_token(token: str) -> str:
        lowered = token.strip().lower()
        if lowered in {"yes", "no"}:
            return lowered.capitalize()
        return token

    def _has_answer(self, question: SheetQuestionRecord) -> bool:
        if question.qid not in self._answers:
            return False
        return bool(self._value_tokens(self._answers[question.qid]))

    def _status_payload(self, question: SheetQuestionRecord) -> tuple[str, str]:
        if self._status_question != question.qid:
            state = "saved" if self._has_answer(question) else "waiting"
        else:
            state = self._status_state

        if state == "saved":
            return STATUS_ICON_SAVED, STATUS_TEXT_SAVED
        if state == "error":
            return STATUS_ICON_ERROR, self._status_error_text(question)
        return STATUS_ICON_WAITING, STATUS_TEXT_WAITING

    def _status_error_text(self, question: SheetQuestionRecord) -> str:
        qtype = (question.qtype or "").lower()
        if qtype.startswith("number"):
            hint = (self._status_hint or "Use a number like 12.6M (no commas)."
                    ).strip()
            if hint:
                return STATUS_TEXT_GENERIC.format(hint=hint)
            return STATUS_TEXT_NUMBER
        if qtype.startswith("single-select") or qtype.startswith("multi-select"):
            return STATUS_TEXT_SELECT
        if qtype.startswith("bool"):
            return STATUS_TEXT_SELECT
        if qtype.startswith("paragraph") or qtype.startswith("short"):
            if self._status_hint:
                return STATUS_TEXT_GENERIC.format(hint=self._status_hint)
            limit = question.maxlen or 300
            return STATUS_TEXT_GENERIC.format(hint=f"Text only, up to {limit} chars.")
        hint = (self._status_hint or "Check the format.").strip()
        return STATUS_TEXT_GENERIC.format(hint=hint)

    def _answers_by_qid(self) -> dict[str, Any]:
        return dict(self._answers)

    async def _store_answer(
        self,
        question: SheetQuestionRecord,
        value: Any,
        *,
        source_message_id: int | None = None,
        advance_if_hidden: bool = True,
    ) -> None:
        self._answers[question.qid] = value
        if question not in self._answer_order:
            self._answer_order.append(question)
        str_value = self._value_for_store(value)
        try:
            self.controller._record_rolling_answer(self.thread_id, question, str_value)
        except Exception:
            pass
        captured = getattr(self.controller, "_captured_msgs", None)
        if (
            captured is not None
            and self.thread_id is not None
            and source_message_id is not None
        ):
            try:
                captured.setdefault(int(self.thread_id), []).append(int(source_message_id))
            except Exception:
                pass
        self._log_event("✅", "answer")
        previous_visibility = dict(self._visibility)
        self._recompute_visibility()

        new_state = self._visible_state(question.qid)
        if self._visibility_flipped(previous_visibility):
            self._note = MAP_CHANGED_NOTE

        if advance_if_hidden and new_state == "skip":
            self._status_question = None
            self._status_state = "waiting"
            await self._advance(direction=1)
            return

        self._waiting = False
        self._status_question = question.qid
        self._status_state = "saved"
        self._status_hint = None
        await self._render_step()

    def _visibility_flipped(self, previous: dict[str, dict[str, str]]) -> bool:
        for qid, entry in self._visibility.items():
            state = entry.get("state")
            prior_state = (previous.get(qid) or {}).get("state")
            if state == prior_state:
                continue
            if state == "skip" or prior_state == "skip":
                return True
        return False

    async def _advance(self, *, direction: int) -> None:
        if direction == 0:
            await self._render_step()
            return

        if direction > 0:
            answers_by_qid = self._answers_by_qid()
            try:
                jump = rules.next_index_by_rules(
                    self._current_index, list(self._steps), answers_by_qid
                )
            except Exception:
                jump = None
            if jump is None:
                start = self._current_index + 1
                seek_direction = 1
            else:
                start = jump
                seek_direction = 1 if jump >= self._current_index else -1
        else:
            start = self._current_index - 1
            seek_direction = -1

        next_index = self._seek_visible_index(start, seek_direction)
        if next_index is None:
            if direction > 0:
                self._current_index = len(self._steps)
                await self._finish()
            return

        self._current_index = next_index
        self._current_question = None
        self._status_question = None
        self._status_state = "waiting"
        self._status_hint = None
        self._waiting = False
        await self._render_step()

    async def handle_message(self, message: discord.Message) -> bool:
        if self._closed or self._current_question is None:
            return False
        if self.thread_id is not None:
            channel_identifier = getattr(message.channel, "id", None)
            try:
                if int(channel_identifier) != self.thread_id:
                    return False
            except (TypeError, ValueError):
                return False
        if not self._is_owner(message.author):
            return False
        raw = (message.content or "").strip()
        question = self._current_question
        qtype_value = (question.qtype or getattr(question, "type", "") or "").lower()
        if qtype_value not in TEXT_TYPES:
            return False
        ok, cleaned, hint = self._validate(question, raw)
        if not ok:
            self._status_question = question.qid
            self._status_state = "error"
            self._status_hint = hint or None
            self._waiting = True
            await self._render_step()
            self._log_event("❌", "invalid", detail=hint)
            return True
        message_id = getattr(message, "id", None)
        deleted = False
        try:
            await message.delete()
            deleted = True
        except Exception:
            deleted = False
        await self._store_answer(
            question,
            cleaned,
            source_message_id=None if deleted else message_id,
        )
        return True

    def _log_event(self, emoji: str, event: str, *, detail: str | None = None) -> None:
        if self.thread_id is None:
            return
        guild_obj = self.guild or getattr(self.thread, "guild", None)
        launch_log.info(
            "%s Onboarding — %s • user=%s • thread=%s • qid=%s%s",
            emoji,
            event,
            user_label(guild_obj, self.owner_id),
            channel_label(guild_obj, self.thread_id),
            getattr(self._current_question, "qid", "unknown"),
            f" • {detail}" if detail else "",
        )

    async def _finish(self) -> None:
        self._current_question = None
        self._status_question = None
        self._status_state = "saved"
        self._status_hint = None
        self._waiting = False
        self._recompute_visibility()

        items: list[tuple[str, str]] = []
        for question in self._all_questions:
            entry = self._visibility.get(question.qid, {})
            state = entry.get("state") or ("show" if question.required else "optional")
            visible = entry.get("visible", state != "skip")
            required = bool(entry.get("required", question.required))
            if not visible or state == "skip":
                continue
            if not self._has_answer(question):
                if state == "optional" and not required:
                    continue
                continue
            answer = self._value_for_summary(self._answers.get(question.qid))
            if not answer:
                continue
            label = question.label or question.qid
            items.append((label, answer))

        try:
            await self.card.render_summary(items=items)
        except Exception:
            message = await self.card.ensure()
            try:
                await message.edit(content="**Onboarding — Summary**", view=None)
            except Exception:
                pass
        await self._cleanup_captured_messages()
        self._closed = True
        self.controller._complete_rolling(self.thread_id)

    async def _cleanup_captured_messages(self) -> None:
        if self.thread_id is None:
            return
        if not get_onboarding_cleanup_after_summary():
            return
        captured = getattr(self.controller, "_captured_msgs", None)
        if not isinstance(captured, dict):
            return
        thread_key = int(self.thread_id)
        message_ids = list(captured.get(thread_key, []))
        if not message_ids:
            captured.pop(thread_key, None)
            return
        fetcher = getattr(self.thread, "fetch_message", None)
        if not callable(fetcher):
            captured.pop(thread_key, None)
            return
        for message_id in message_ids:
            try:
                fetched = await fetcher(message_id)
            except Exception:
                continue
            try:
                await fetched.delete()
            except Exception:
                pass
        captured.pop(thread_key, None)

    def _validate(
        self, question: SheetQuestionRecord, value: str
    ) -> tuple[bool, str, str | None]:
        if (question.qtype or "").lower().startswith("number"):
            return self._validate_number(question, value)
        return self._validate_text(question, value)

    def _validate_text(
        self, question: SheetQuestionRecord, value: str
    ) -> tuple[bool, str, str | None]:
        cleaned = value.strip()
        maxlen = question.maxlen or 0
        if maxlen and len(cleaned) > maxlen:
            return False, cleaned, f"Max {maxlen} characters."
        validate = (question.validate or "").strip()
        if validate.lower().startswith("regex:"):
            pattern = validate.split(":", 1)[1].strip()
            if pattern:
                try:
                    if not re.fullmatch(pattern, cleaned):
                        return False, cleaned, "Format doesn’t match expected pattern."
                except re.error:
                    log.warning(
                        "rolling_card: invalid regex",
                        extra={"qid": question.qid, "pattern": pattern},
                    )
        return True, cleaned, None

    def _validate_number(
        self, question: SheetQuestionRecord, value: str
    ) -> tuple[bool, str, str | None]:
        cleaned = value.strip()
        if not cleaned:
            return False, cleaned, "Numbers only (e.g., 71)."
        enforce_int, min_value, max_value, step = self._parse_numeric_bounds(question.validate)
        try:
            number = int(cleaned) if enforce_int else float(cleaned)
        except ValueError:
            return False, cleaned, "Numbers only (e.g., 71)."
        if (min_value is not None and number < min_value) or (
            max_value is not None and number > max_value
        ):
            if min_value is not None and max_value is not None:
                return (
                    False,
                    cleaned,
                    f"Number must be between {self._fmt_number(min_value, enforce_int)} and {self._fmt_number(max_value, enforce_int)}.",
                )
            if min_value is not None:
                return (
                    False,
                    cleaned,
                    f"Number must be at least {self._fmt_number(min_value, enforce_int)}.",
                )
            return (
                False,
                cleaned,
                f"Number must be at most {self._fmt_number(max_value, enforce_int)}.",
            )
        if step and min_value is not None:
            base = min_value
        else:
            base = 0
        if step:
            remainder = (number - base) / step
            if not math.isclose(remainder, round(remainder), rel_tol=1e-9, abs_tol=1e-9):
                return False, cleaned, f"Number must increase by steps of {self._fmt_number(step, enforce_int)}."
        return True, cleaned, None

    @staticmethod
    def _fmt_number(value: float, enforce_int: bool) -> str:
        if enforce_int:
            return str(int(value))
        return ("%g" % value).rstrip("0").rstrip(".") if isinstance(value, float) else str(value)

    @staticmethod
    def _parse_numeric_bounds(
        validate: str | None,
    ) -> tuple[bool, float | None, float | None, float | None]:
        if not validate:
            return False, None, None, None
        text = validate.strip()
        enforce_int = text.lower().startswith("int")
        remainder = text
        if ":" in text:
            prefix, suffix = text.split(":", 1)
            if prefix.lower() == "int":
                remainder = suffix
        tokens = re.split(r"[;,]", remainder)
        min_value: float | None = None
        max_value: float | None = None
        step: float | None = None
        for token in tokens:
            token = token.strip()
            if not token:
                continue
            match = re.match(r"(?i)(min|max|step)\s*=\s*(-?\d+(?:\.\d+)?)", token)
            if not match:
                continue
            key = match.group(1).lower()
            try:
                value = float(match.group(2))
            except ValueError:
                continue
            if key == "min":
                min_value = value
            elif key == "max":
                max_value = value
            elif key == "step":
                step = value
        return enforce_int, min_value, max_value, step
async def locate_welcome_message(thread: discord.Thread) -> discord.Message | None:
    """Return the welcome greeting message for ``thread`` when available."""

    starter = getattr(thread, "starter_message", None)
    if starter is not None:
        return starter

    parent = getattr(thread, "parent", None)
    if parent is not None:
        parent_fetch = getattr(parent, "fetch_message", None)
        if callable(parent_fetch):
            try:
                return await parent_fetch(int(thread.id))
            except Exception:
                pass

    history = getattr(thread, "history", None)
    if callable(history):
        try:
            async for candidate in history(limit=5, oldest_first=True):
                return candidate
        except Exception:
            pass

    return None


def _is_welcome_trigger_message(
    message: discord.Message | None,
    *,
    bot_user_id: int | None = None,
) -> bool:
    if message is None:
        return False
    author = getattr(message, "author", None)
    if author is None:
        return False
    content = (getattr(message, "content", "") or "").lower()
    has_stable_intro = "welcome to c1c" in content and "slap a 👍 on this message" in content
    has_legacy_intro = "awake by reacting with" in content or "[#welcome:ticket]" in content
    if has_stable_intro:
        return True
    if bot_user_id is not None and getattr(author, "id", None) != bot_user_id:
        return False
    return has_legacy_intro


async def locate_welcome_trigger_message(
    thread: discord.Thread,
    *,
    bot_user_id: int | None = None,
    preferred_message: discord.Message | None = None,
) -> discord.Message | None:
    """Locate the bot-authored welcome trigger/intro message in the thread."""

    if _is_welcome_trigger_message(preferred_message, bot_user_id=bot_user_id):
        return preferred_message

    history = getattr(thread, "history", None)
    if callable(history):
        try:
            async for candidate in history(limit=25, oldest_first=False):
                if _is_welcome_trigger_message(candidate, bot_user_id=bot_user_id):
                    return candidate
        except Exception:
            pass
    return None


def extract_target_from_message(
    message: discord.Message | None,
) -> tuple[int | None, int | None]:
    """Return ``(target_user_id, message_id)`` extracted from ``message``."""

    target_id: int | None = None
    message_id: int | None = None

    if message is None:
        return target_id, message_id

    raw_message_id = getattr(message, "id", None)
    if raw_message_id is not None:
        try:
            message_id = int(raw_message_id)
        except (TypeError, ValueError):
            message_id = None

    for user in getattr(message, "mentions", []) or []:
        if getattr(user, "bot", False):
            continue
        user_id = getattr(user, "id", None)
        if user_id is None:
            continue
        try:
            target_id = int(user_id)
        except (TypeError, ValueError):
            continue
        break

    if target_id is None:
        author = getattr(message, "author", None)
        if author is not None and not getattr(author, "bot", False):
            try:
                target_id = int(getattr(author, "id", None))
            except (TypeError, ValueError):
                target_id = None

    return target_id, message_id


class BaseWelcomeController:
    """Shared orchestration logic for welcome/promo onboarding flows."""

    def __init__(self, bot: commands.Bot, *, flow: str) -> None:
        self.bot = bot
        self.flow = flow
        self._ready_evt = asyncio.Event()
        self._threads: Dict[int, discord.Thread] = {}
        self._questions: Dict[int, list[Question]] = {}
        self.questions_by_thread = self._questions
        self.answers_by_thread: Dict[int, dict[str, Any]] = {}
        self._select_messages: Dict[int, discord.Message] = {}
        self._preview_messages: Dict[int, discord.Message] = {}
        self._preview_logged: set[int] = set()
        self._sources: Dict[int, str] = {}
        self._allowed_users: Dict[int, set[int]] = {}
        self._panel_messages: Dict[int, int] = {}
        self._prefetched_panels: Dict[int, discord.Message] = {}
        self._initiators: Dict[int, discord.abc.User | discord.Member | None] = {}
        self._target_users: Dict[int, int | None] = {}
        self._target_message_ids: Dict[int, int | None] = {}
        self.retry_message_ids: Dict[int, int] = {}
        self._inline_messages: Dict[int, discord.Message] = {}
        self._inline_message_ids: Dict[int, int] = {}
        self._next_prompt_message_ids: Dict[int, int] = {}
        self.answers_by_thread: Dict[int | None, dict[str, Any]] = {}
        # Shared session-like state for UI helpers that need to persist
        # per-thread metadata (e.g., the active panel message id).
        self.session: dict[str, object] = {"_PANEL_MESSAGES": self._panel_messages}
        self._panel_manager = PanelMessageManager(self.session)
        recruiter_attr = getattr(self, "recruiter_role_ids", None) or getattr(self, "RECRUITER_ROLE_IDS", None)
        if recruiter_attr:
            self.recruiter_role_ids = list(recruiter_attr)
        else:
            try:
                self.recruiter_role_ids = list(get_recruiter_role_ids())
            except Exception:
                self.recruiter_role_ids = []

    async def _send_welcome_summary_safe(
        self,
        *,
        thread: discord.abc.Messageable,
        answers: Mapping[str, Any],
        author: discord.abc.User | discord.Member | None,
        schema_hash: str | None,
        visibility: Mapping[str, Any] | None,
        content: str | None = None,
        allowed_mentions: discord.AllowedMentions | None = None,
    ) -> bool:
        """Build and send welcome summary; fall back to a minimal embed on error."""

        try:
            embed = build_summary_embed(
                flow=self.flow,
                answers=answers,
                author=author,
                schema_hash=schema_hash,
                visibility=visibility,
            )
        except Exception:
            log.error(
                "onboarding.summary.build_failed_unexpected",
                exc_info=True,
                extra={"flow": self.flow, "thread_id": getattr(thread, "id", None)},
            )
            fallback_author = author if isinstance(author, discord.Member) else None
            embed = _fallback_welcome_embed(fallback_author)

        try:
            await thread.send(
                content=content,
                embed=embed,
                allowed_mentions=allowed_mentions,
            )
        except Exception:
            log.error(
                "onboarding.summary.send_failed",
                exc_info=True,
                extra={"flow": self.flow, "thread_id": getattr(thread, "id", None)},
            )
            return False

        return True

    def _resolve_applicant_id(
        self, thread_id: int, session: SessionData | None = None
    ) -> int | None:
        target_id = self._target_users.get(thread_id)
        if target_id is not None:
            try:
                return int(target_id)
            except (TypeError, ValueError):
                return None

        thread = self._thread_for(thread_id)
        owner = getattr(thread, "owner", None)
        owner_id = getattr(owner, "id", None) or getattr(thread, "owner_id", None)
        if owner_id is not None:
            try:
                return int(owner_id)
            except (TypeError, ValueError):
                pass

        respondent = getattr(session, "respondent_id", None)
        if respondent is not None:
            try:
                return int(respondent)
            except (TypeError, ValueError):
                return None
        return None

    def _final_step_index(self, thread_id: int, session: SessionData | None) -> int:
        visible_indices = self._visible_indices(thread_id)
        if visible_indices:
            return max(visible_indices)

        questions = self._questions.get(thread_id) or []
        if questions:
            return max(len(questions) - 1, 0)

        try:
            if session and session.current_question_index is not None:
                return max(int(session.current_question_index), 0)
        except (TypeError, ValueError):
            pass
        return 0

    async def _persist_session_start(
        self,
        thread_id: int,
        *,
        panel_message_id: int | None,
        session_data: SessionData | None = None,
        panel_created_at: datetime | None = None,
    ) -> None:
        applicant_id = self._resolve_applicant_id(thread_id, session_data)
        if applicant_id is None:
            return

        try:
            sheet_session = await ensure_session_for_thread(
                applicant_id,
                thread_id,
                updated_at=panel_created_at,
            )
        except Exception:
            log.warning("welcome:persist_start_failed", exc_info=True)
            return

        if panel_message_id is not None:
            sheet_session.panel_message_id = panel_message_id
        sheet_session.completed = False
        sheet_session.step_index = 0
        sheet_session._touch(timestamp=panel_created_at or utc_now())

        try:
            sheet_session.save_to_sheet()
        except Exception:
            log.warning("welcome:sheet_start_save_failed", exc_info=True)

    async def _persist_session_completion(
        self,
        thread_id: int,
        *,
        session_data: SessionData | None,
        answers: Mapping[str, Any],
        panel_message_id: int | None,
    ) -> None:
        applicant_id = self._resolve_applicant_id(thread_id, session_data)
        if applicant_id is None:
            return

        try:
            sheet_session = await ensure_session_for_thread(applicant_id, thread_id)
        except Exception:
            log.warning("welcome:persist_complete_failed", exc_info=True)
            return

        if panel_message_id is not None:
            sheet_session.panel_message_id = panel_message_id
        sheet_session.step_index = self._final_step_index(thread_id, session_data)
        sheet_session.answers = dict(answers)
        sheet_session.mark_completed()
        sheet_session._touch()

        try:
            sheet_session.save_to_sheet()
        except Exception:
            log.warning("welcome:sheet_complete_save_failed", exc_info=True)

    async def wait_until_ready(self, timeout: float = 0.0) -> bool:
        if timeout and timeout > 0:
            try:
                await asyncio.wait_for(self._ready_evt.wait(), timeout=timeout)
                return True
            except asyncio.TimeoutError:
                return self._ready_evt.is_set()
        await self._ready_evt.wait()
        return True

    async def log_event(self, level: str, event: str, **fields: Any) -> None:
        if diag.is_enabled():
            await diag.log_event(level, event, **fields)

    def _update_thread_visibility(
        self, thread_id: int | None, visibility: Mapping[str, Mapping[str, Any]]
    ) -> None:
        if thread_id is None:
            return
        session = store.get(thread_id)
        if session is None:
            return
        session.visibility = dict(visibility)

    def get_questions(self, thread_id: int | None) -> list[dict[str, Any]]:
        return [
            {"id": "ign", "label": "Your in-game name", "kind": "text"},
            {
                "id": "vibe",
                "label": "Preferred clan vibe",
                "kind": "choice",
                "choices": ["Casual", "Balanced", "Competitive"],
            },
            {"id": "tz", "label": "Timezone (e.g., CET, PST)", "kind": "tz"},
        ]

    def render_step(self, thread_id: int | None, step: int) -> str:
        questions = self.get_questions(thread_id)
        step = max(0, min(step, len(questions)))
        key = int(thread_id) if thread_id is not None else None
        answers = self.answers_by_thread.setdefault(key, {})
        if step >= len(questions):
            return "Reviewing your answers…"
        question = questions[step]
        current = answers.get(question["id"])
        hint = f"\n\n_Current:_ **{current}**" if current else ""
        return f"**Step {step + 1} of {len(questions)}**\n{question['label']}{hint}"

    async def capture_step(self, interaction: discord.Interaction, thread_id: int | None, step: int) -> None:
        return None

    def is_finished(self, thread_id: int | None, step: int) -> bool:
        return step >= len(self.get_questions(thread_id))

    async def set_answer(self, thread_id: int | None, key: str, value: str) -> None:
        dictionary = self.answers_by_thread.setdefault(int(thread_id) if thread_id is not None else None, {})
        dictionary[key] = value

    async def finish_and_summarize(self, interaction: discord.Interaction, thread_id: int | None) -> None:
        from discord import AllowedMentions

        key = int(thread_id) if thread_id is not None else None
        answers = dict(self.answers_by_thread.get(key, {}))
        user = getattr(interaction, "user", None)
        embed = self._make_summary_embed(user, answers, interaction.channel)

        await interaction.response.edit_message(content="✅ Collected. Posting summary…", view=None)

        role_ids = [rid for rid in (self.recruiter_role_ids or []) if rid]
        mentions = " ".join(f"<@&{rid}>" for rid in role_ids)

        await interaction.channel.send(
            content=mentions or None,
            embed=embed,
            allowed_mentions=AllowedMentions(everyone=False, users=False, roles=True),
        )

        if key in self.answers_by_thread:
            del self.answers_by_thread[key]

        await self.log_event("info", "onboard_finished", thread_id=thread_id, pinged_roles=len(role_ids))

    def _make_summary_embed(
        self,
        user: discord.abc.User | discord.Member | None,
        answers: dict[str, Any],
        thread: discord.abc.MessageableChannel | None,
    ) -> discord.Embed:
        embed = discord.Embed(title="New Onboarding", description=f"Applicant: {getattr(user, 'mention', 'unknown')}")
        embed.add_field(name="IGN", value=answers.get("ign", "—"), inline=True)
        embed.add_field(name="Clan vibe", value=answers.get("vibe", "—"), inline=True)
        embed.add_field(name="Timezone", value=answers.get("tz", "—"), inline=True)
        try:
            embed.add_field(name="Thread", value=f"[Open thread]({thread.jump_url})", inline=False)
        except Exception:
            pass
        embed.set_footer(text="C1C Onboarding")
        return embed

    async def _build_and_send_summary(
        self,
        *,
        thread: discord.Thread | discord.TextChannel,
        flow: str,
        answers: Mapping[str, Any],
        visibility: Mapping[str, Any] | None,
        author: discord.abc.User | discord.Member | None,
        schema_hash: str | None,
        mention_text: str | None = None,
        allowed_mentions: discord.AllowedMentions | None = None,
    ) -> tuple[discord.Message, discord.Embed]:
        summary_embed = build_summary_embed(
            flow,
            answers,
            author,
            schema_hash or "",
            visibility,
        )

        view: discord.ui.View | None = None
        if flow == "welcome" and _is_fallback_summary(summary_embed):
            view = RetryWelcomeSummaryView(thread_id=thread.id)

        summary_message = await thread.send(
            content=mention_text or None,
            embed=summary_embed,
            view=view,
            allowed_mentions=allowed_mentions if mention_text else None,
        )

        return summary_message, summary_embed

    def _thread_for(self, thread_id: int) -> discord.Thread | None:
        return self._threads.get(thread_id)

    def diag_target_user_id(self, thread_id: int) -> int | None:
        """Return the cached target recruit identifier for diagnostics."""

        return self._target_users.get(thread_id)

    @staticmethod
    def _question_key(question: Question | dict[str, Any]) -> str:
        if hasattr(question, "qid"):
            value = getattr(question, "qid")
            return str(value)
        if isinstance(question, dict):
            candidate = question.get("id") or question.get("qid")
            if candidate is not None:
                return str(candidate)
        return ""

    def _question_meta(self, question: Question | dict[str, Any]) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "qid": self._question_key(question),
            "label": "",
            "type": "",
            "validate": None,
            "help": None,
            "note": None,
        }
        if isinstance(question, dict):
            meta["label"] = str(question.get("label") or question.get("text") or "")
            meta["type"] = str(
                question.get("type")
                or question.get("qtype")
                or question.get("kind")
                or ""
            )
            meta["validate"] = question.get("validate")
            meta["help"] = question.get("help")
            meta["note"] = question.get("note")
        else:
            meta["label"] = str(getattr(question, "label", ""))
            meta["type"] = str(
                getattr(question, "type", None)
                or getattr(question, "qtype", None)
                or ""
            )
            meta["validate"] = getattr(question, "validate", None)
            meta["help"] = getattr(question, "help", None)
            meta["note"] = getattr(question, "note", None)
        return meta

    def _has_sheet_regex(self, meta: dict[str, Any]) -> bool:
        return bool(_sheet_regex(meta))

    @staticmethod
    def _question_type_value(question: Question | dict[str, Any]) -> str:
        if isinstance(question, dict):
            value = question.get("type") or question.get("qtype") or question.get("kind")
        else:
            value = getattr(question, "type", None)
            if not value:
                value = getattr(question, "qtype", None)
        return str(value or "")

    @staticmethod
    def _question_options(question: Question | dict[str, Any]) -> list[Any]:
        raw: Any
        if isinstance(question, dict):
            raw = question.get("options") or question.get("choices")
        else:
            raw = getattr(question, "options", None)
        if isinstance(raw, Sequence):
            return list(raw)
        qtype = BaseWelcomeController._question_type_value(question).strip().lower()
        if not qtype.startswith("single-select") and not qtype.startswith("multi-select"):
            return []
        if isinstance(question, dict):
            note = question.get("note")
        else:
            note = getattr(question, "note", None)
        tokens: list[str] = []
        if isinstance(note, str) and note.strip():
            tokens = [token.strip() for token in note.replace("\n", ",").split(",") if token.strip()]
        elif isinstance(question, dict):
            validate = question.get("validate")
            if isinstance(validate, str) and validate.strip():
                tokens = [token.strip() for token in validate.replace("\n", ",").split(",") if token.strip()]
        else:
            validate = getattr(question, "validate", None)
            if isinstance(validate, str) and validate.strip():
                tokens = [token.strip() for token in validate.replace("\n", ",").split(",") if token.strip()]
        if tokens:
            return [{"label": token, "value": token} for token in tokens]
        return []

    def validate_answer(self, meta: dict[str, Any], raw: str) -> tuple[bool, str | None, str | None]:
        return validate_answer(meta, raw)

    async def _send_validation_error(
        self,
        interaction: discord.Interaction,
        thread_id: int,
        meta: dict[str, Any],
        message: str | None,
    ) -> None:
        label = meta.get("label") or meta.get("qid") or "This question"
        notice = message or "Input does not match the required format."
        await _safe_ephemeral(interaction, f"⚠️ **{label}** • {notice}")

    def _answer_for(self, thread_id: int, key: str) -> Any:
        answers = self.answers_by_thread.get(thread_id)
        if answers and key in answers:
            return answers[key]
        session = store.get(thread_id)
        if session and session.answers:
            return session.answers.get(key)
        return None

    def _answer_present(self, question: Question | dict[str, Any], value: Any) -> bool:
        if value is None:
            return False
        qtype = self._question_type_value(question).strip().lower()
        if qtype in SELECT_TYPES:
            return _has_select_answer(value)
        if qtype == "number":
            return value is not None
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (int, float)):
            return True
        if isinstance(value, dict):
            return bool(value)
        if isinstance(value, Iterable):
            return any(True for _ in value)
        return bool(value)

    @staticmethod
    def _canonical_bool(value: Any) -> str | None:
        if isinstance(value, bool):
            return "yes" if value else "no"
        if isinstance(value, str):
            text = value.strip().lower()
            if not text:
                return None
            if text in {"yes", "y", "true", "1"}:
                return "yes"
            if text in {"no", "n", "false", "0"}:
                return "no"
        if isinstance(value, (int, float)):
            return "yes" if value else "no"
        return None

    def _question_for(self, thread_id: int, key: str) -> Question | dict[str, Any] | None:
        questions = self._questions.get(thread_id) or []
        for question in questions:
            if self._question_key(question) == key:
                return question
        return None

    async def set_answer(self, thread_id: int, key: str, value: Any) -> None:
        answers = self.answers_by_thread.setdefault(thread_id, {})
        question = self._question_for(thread_id, key)
        qtype = self._question_type_value(question).strip().lower() if question is not None else ""
        normalized = value
        if isinstance(value, str):
            text = value.strip()
            normalized = text if text else None
        if qtype == "bool":
            token = self._canonical_bool(value)
            normalized = token if token is not None else (normalized if isinstance(normalized, str) else normalized)
        if normalized is None or (isinstance(normalized, str) and not normalized.strip()):
            answers.pop(key, None)
        elif isinstance(normalized, (list, tuple)) and not normalized:
            answers.pop(key, None)
        elif isinstance(normalized, dict) and not normalized:
            answers.pop(key, None)
        else:
            answers[key] = normalized

        session = store.get(thread_id)
        if session is not None:
            session.answers = session.answers or {}
            if normalized is None or (isinstance(normalized, str) and not normalized.strip()):
                session.answers.pop(key, None)
            elif isinstance(normalized, (list, tuple)) and not normalized:
                session.answers.pop(key, None)
            elif isinstance(normalized, dict) and not normalized:
                session.answers.pop(key, None)
            else:
                session.answers[key] = normalized
            try:
                session.visibility = rules.evaluate_visibility(
                    self._questions.get(thread_id, []), session.answers
                )
            except Exception:
                log.warning("failed to recompute visibility during inline capture", exc_info=True)

    def has_answer(self, thread_id: int, question: Question | dict[str, Any]) -> bool:
        key = self._question_key(question)
        if not key:
            return False
        value = self._answer_for(thread_id, key)
        return self._answer_present(question, value)

    def _visibility_map(self, thread_id: int) -> dict[str, dict[str, str]]:
        session = store.get(thread_id)
        if session is None:
            return {}
        return session.visibility or {}

    def _visible_indices(self, thread_id: int) -> list[int]:
        questions = self._questions.get(thread_id) or []
        visibility = self._visibility_map(thread_id)
        indices: list[int] = []
        for idx, question in enumerate(questions):
            qid = self._question_key(question)
            if not qid:
                indices.append(idx)
                continue
            if _visible_state(visibility, qid) != "skip":
                indices.append(idx)
        return indices

    def resolve_step(
        self,
        thread_id: int,
        step: int,
        *,
        direction: int = 1,
    ) -> tuple[int | None, Question | dict[str, Any] | None]:
        questions = self._questions.get(thread_id) or []
        if not questions:
            return None, None

        total = len(questions)
        if direction >= 0 and step >= total:
            return None, None
        if direction < 0 and step < 0:
            return None, None

        if step < 0:
            idx = 0 if direction >= 0 else total - 1
        elif step >= total:
            idx = total - 1
        else:
            idx = step

        visibility = self._visibility_map(thread_id)
        while 0 <= idx < total:
            question = questions[idx]
            qid = self._question_key(question)
            if not qid or _visible_state(visibility, qid) != "skip":
                return idx, question
            idx += 1 if direction >= 0 else -1
        return None, None

    def _set_current_step_for_thread(
        self, thread_id: int, step_index: int
    ) -> SessionData | None:
        session = store.get(thread_id)
        if session is None:
            return None

        questions = self._questions.get(thread_id) or []
        if not questions or step_index < 0 or step_index >= len(questions):
            return None

        pending_step = {"kind": "inline", "index": step_index}
        session.current_question_index = step_index
        session.pending_step = pending_step
        store.set_pending_step(thread_id, pending_step)
        return session

    def next_visible_step(self, thread_id: int, current_step: int) -> int | None:
        questions = self._questions.get(thread_id) or []
        if not questions:
            return None

        answers = self.answers_by_thread.get(thread_id, {})
        start_index = current_step + 1
        direction = 1

        try:
            jump = rules.next_index_by_rules(current_step, list(questions), answers)
        except Exception:
            jump = None

        if jump is not None:
            start_index = jump
            direction = 1 if jump >= current_step else -1

        next_index, _ = self.resolve_step(thread_id, start_index, direction=direction)
        return next_index

    def previous_visible_step(self, thread_id: int, current_step: int) -> int | None:
        prev_index, _ = self.resolve_step(thread_id, current_step - 1, direction=-1)
        return prev_index

    def _progress_label(self, thread_id: int, index: int) -> str:
        visible = self._visible_indices(thread_id)
        if not visible:
            total = max(len(self._questions.get(thread_id) or []), 1)
            return f"{index + 1}/{total}"
        try:
            position = visible.index(index)
        except ValueError:
            total = max(len(visible), 1)
            return f"{index + 1}/{total}"
        total = max(len(visible), 1)
        return f"{position + 1}/{total}"

    def is_finished(self, thread_id: int, step: int) -> bool:
        next_index, _ = self.resolve_step(thread_id, step, direction=1)
        return next_index is None

    def session_status(
        self, thread_id: int, question: object | None = None
    ) -> str | None:
        """Return the session status for the given thread/question."""

        session = store.get(thread_id)
        if session is None:
            return None

        if question is not None:
            try:
                key = self._question_key(question)
            except Exception:
                key = None
            if key:
                stored = self._answer_for(thread_id, key)
                if stored is not None:
                    return "answered"

        status = getattr(session, "status", None)
        if isinstance(status, str) and status.strip():
            return status.strip()
        return None

    def render_step(self, thread_id: int, step: int) -> str:
        questions = self._questions.get(thread_id) or []
        status = self.session_status(thread_id)
        if not questions:
            if self.is_session_completed(thread_id) or (
                status is not None and status not in {"idle", "in_progress"}
            ):
                return "All onboarding questions are complete."
            return "No onboarding questions are configured for this flow yet."

        resolved_index, question = self.resolve_step(thread_id, step, direction=1)
        if resolved_index is None or question is None:
            return "All onboarding questions are complete."

        label = getattr(question, "label", None)
        if isinstance(question, dict):
            label = question.get("label") or question.get("text") or label
        label = label or str(question)

        key = self._question_key(question)
        stored = self._answer_for(thread_id, key)
        formatted = _preview_value_for_question(question, stored)
        progress = self._progress_label(thread_id, resolved_index)

        header = f"**Onboarding • {progress}**"

        lines = [header, f"## {label}"]

        help_text = getattr(question, "help", None)
        if isinstance(question, dict):
            help_text = question.get("help") or help_text
        qtype_value = self._question_type_value(question).strip().lower()
        reply_hint = "Just reply in this thread with your answer." if qtype_value in TEXT_TYPES else None
        if help_text:
            lines.append(f"_{help_text}_")
        if reply_hint:
            lines.append(reply_hint)

        if formatted:
            lines.append(f"**Current answer:** {formatted}")

        return "\n\n".join(lines)

    def build_panel_content(self, thread_id: int, step: int) -> str:
        """Render the full panel content (header + question block) from scratch."""

        header = self._modal_intro_text()
        question_block = self.render_step(thread_id, step)
        if header and question_block:
            return f"{header}\n\n{question_block}"
        return question_block or header or ""

    async def finish_inline_wizard(
        self,
        thread_id: int,
        interaction: discord.Interaction,
        *,
        message: discord.Message | None = None,
    ) -> None:
        session = store.get(thread_id)
        if session is not None:
            session.status = "completed"
            session.current_question_index = None
        answers: dict[str, Any] = {}
        if session is not None and session.answers:
            answers.update(session.answers)
        inline_answers = self.answers_by_thread.get(thread_id, {})
        if inline_answers:
            answers.update(inline_answers)
        if message is None:
            message = getattr(interaction, "message", None)
        notice = "✅ Collected. Posting summary…"
        try:
            if interaction is not None and not interaction.response.is_done():
                await interaction.response.edit_message(content=notice, view=None)
            elif message is not None:
                await message.edit(content=notice, view=None)
        except Exception:
            log.warning("failed to update wizard completion message", exc_info=True)

        inline_map = getattr(self, "_inline_messages", None)
        if isinstance(inline_map, dict):
            inline_map.pop(thread_id, None)
        id_map = getattr(self, "_inline_message_ids", None)
        if isinstance(id_map, dict):
            id_map.pop(thread_id, None)

        thread: discord.Thread | None = None
        if message is not None:
            channel = getattr(message, "channel", None)
            if isinstance(channel, discord.Thread):
                thread = channel
        if thread is None:
            thread = self._threads.get(thread_id)
        if thread is None:
            return

        session = store.get(thread_id)
        visibility = None
        if session is not None:
            session.answers = dict(answers)
            visibility = session.visibility
        else:
            session = store.ensure(thread_id, flow=self.flow, schema_hash=schema_hash(self.flow))
            session.answers = dict(answers)
            visibility = session.visibility

        try:
            summary_author = self._resolve_summary_author(thread, interaction)
        except Exception:
            log.warning("failed to resolve summary author; falling back to thread owner", exc_info=True)
            summary_author = getattr(thread, "owner", None) or interaction.user

        schema = session.schema_hash if session else schema_hash(self.flow)
        recruiter_ids = list(getattr(self, "recruiter_role_ids", []) or [])
        if not recruiter_ids:
            recruiter_ids = list(getattr(self, "RECRUITER_ROLE_IDS", []) or [])
        mention_text = " ".join(f"<@&{int(role_id)}>" for role_id in recruiter_ids if role_id)
        allowed_mentions = discord.AllowedMentions(roles=True, users=False, everyone=False)
        schema = session.schema_hash if session else schema_hash(self.flow)
        summary_success = await self._send_welcome_summary_safe(
            thread=thread,
            answers=dict(answers),
            author=summary_author,
            schema_hash=schema or "",
            visibility=visibility,
            content=mention_text or None,
            allowed_mentions=allowed_mentions if mention_text else None,
        )

        if summary_success:
            await self._log_panel_completion(
                thread_id,
                thread=thread,
                actor=getattr(interaction, "user", None),
                session=session,
                answers=dict(answers),
            )

            await self._persist_session_completion(
                thread_id,
                session_data=session,
                answers=dict(answers),
                panel_message_id=self._panel_messages.get(thread_id),
            )

        self.answers_by_thread.pop(thread_id, None)
        self._cleanup_session(
            thread_id,
            preserve_session=bool(summary_embed and _is_fallback_summary(summary_embed)),
        )

    def _log_fields(
        self,
        thread_id: int,
        *,
        actor: discord.abc.User | discord.Member | None | object = ...,
    ) -> dict[str, Any]:
        thread = self._thread_for(thread_id)
        context = logs.thread_context(thread)
        context["flow"] = self.flow
        resolved_actor: discord.abc.User | discord.Member | None
        if actor is ...:
            resolved_actor = self._initiators.get(thread_id)
        else:
            resolved_actor = cast(discord.abc.User | discord.Member | None, actor)
        context["actor"] = logs.format_actor(resolved_actor)
        handle = logs.format_actor_handle(resolved_actor)
        if handle:
            context["actor_name"] = handle
        return context

    async def _log_panel_completion(
        self,
        thread_id: int,
        *,
        thread: discord.Thread | None,
        actor: discord.abc.User | discord.Member | None,
        session: SessionData | None,
        answers: Mapping[str, Any],
    ) -> None:
        question_count = len(self._questions.get(thread_id, [])) if hasattr(self, "_questions") else None
        schema_version = getattr(session, "schema_hash", None)
        extras: dict[str, Any] | None = None
        level_detail = _extract_level_detail(answers)
        if level_detail:
            extras = {"level_detail": level_detail}
        scope = "promo" if str(self.flow or "").startswith("promo") else (self.flow or "welcome")
        await logs.log_onboarding_panel_lifecycle(
            event="complete",
            ticket=thread,
            actor=actor,
            channel=getattr(thread, "parent", None),
            questions=question_count,
            schema_version=schema_version,
            extras=extras,
            scope=scope,
        )

    async def _send_panel_with_retry(
        self,
        thread: discord.Thread,
        *,
        content: str,
        view: discord.ui.View,
    ) -> discord.Message:
        last_error: Exception | None = None
        for delay in (0.0, *_PANEL_RETRY_DELAYS):
            if delay:
                await asyncio.sleep(delay)
            try:
                message = await thread.send(content, view=view)
                if getattr(message, "pinned", False):
                    try:
                        await message.unpin()
                    except Exception:
                        log.warning("failed to unpin welcome panel", exc_info=True)
                return message
            except Exception as exc:  # pragma: no cover - network
                last_error = exc
        assert last_error is not None  # for mypy
        await logs.send_welcome_exception(
            "error",
            last_error,
            **self._log_fields(int(thread.id), result="panel_failed"),
        )
        raise last_error

    async def run(
        self,
        thread: discord.Thread,
        initiator: discord.abc.User | discord.Member | None,
        schema_hash: str | None,
        questions: Sequence[Question],
        *,
        source: str,
    ) -> None:
        thread_id = int(thread.id)
        session = store.ensure(thread_id, flow=self.flow, schema_hash=schema_hash)
        session.answers = session.answers or {}
        session.visibility = rules.evaluate_visibility(questions, session.answers)
        self._threads[thread_id] = thread
        self._questions[thread_id] = list(questions)
        self._sources[thread_id] = source
        self._initiators[thread_id] = initiator
        panels.bind_controller(thread_id, self)

        allowed_ids: set[int] = set()
        self._allowed_users[thread_id] = allowed_ids

        await self._ensure_target_cached(thread_id)
        target_id = self._target_users.get(thread_id)
        if target_id is not None:
            allowed_ids.add(int(target_id))

        await self._start_modal_step(thread, session)
        self._ready_evt.set()

    async def _start_modal_step(
        self,
        thread: discord.Thread,
        session: SessionData,
        *,
        anchor: discord.Message | None = None,
    ) -> None:
        thread_id = int(thread.id)
        if anchor is None:
            anchor = self._prefetched_panels.pop(thread_id, None)
        modals = build_modals(
            self._questions[thread_id],
            session.visibility,
            session.answers,
            title_prefix=self._modal_title_prefix(),
        )
        if not modals:
            await self._start_select_step(thread, session)
            return

        store.set_pending_step(thread_id, {"kind": "inline", "index": 0})
        intro = self._modal_intro_text()
        target_id = self._target_users.get(thread_id)
        view = panels.OpenQuestionsPanelView(
            controller=self,
            thread_id=thread_id,
            target_user_id=target_id,
        )
        message_id = self._panel_messages.get(thread_id)
        message: discord.Message | None = None
        if anchor is not None:
            message = anchor
            anchor_id = getattr(anchor, "id", None)
            if anchor_id is not None:
                try:
                    self._panel_messages[thread_id] = int(anchor_id)
                except (TypeError, ValueError):
                    pass

        parent_channel = getattr(thread, "parent", None)
        try:
            parent_id_int = int(parent_channel.id) if parent_channel is not None else None
        except (TypeError, ValueError, AttributeError):
            parent_id_int = None

        base_log = self._log_fields(thread_id)
        base_log.update(
            {
                "view": "panel",
                "view_tag": panels.WELCOME_PANEL_TAG,
                "custom_id": panels.OPEN_QUESTIONS_CUSTOM_ID,
                "diag": base_log.get("diag") or f"{self.flow}_flow",
                "flow": self.flow,
                "thread_id": thread_id,
            }
        )
        if parent_id_int is not None:
            base_log.setdefault("parent_id", parent_id_int)
            base_log.setdefault("parent_channel_id", parent_id_int)

        prev_id = int(message_id) if message_id else None
        try:
            message = await self._panel_manager.get_or_create(
                thread,
                content=intro,
                view=view,
            )
        except Exception as exc:
            gate_log.exception(
                "send_welcome_exception — failed to post onboarding panel • thread=%s",
                thread.id,
                exc_info=exc,
            )
            await asyncio.sleep(2)
            try:
                message = await self._panel_manager.get_or_create(
                    thread,
                    content=intro,
                    view=view,
                )
            except Exception as retry_exc:
                gate_log.error(
                    "send_welcome_exception — retry failed • thread=%s",
                    thread.id,
                    exc_info=retry_exc,
                )
                raise

        posted_new_message = prev_id is None or int(getattr(message, "id", 0)) != prev_id
        message_id = int(getattr(message, "id", 0))
        self._panel_messages[thread_id] = message_id

        if getattr(message, "pinned", False):
            try:
                await message.unpin()
            except Exception:
                log.warning("failed to unpin welcome panel", exc_info=True)

        panels.register_panel_message(thread_id, message.id)

        panel_log_context = dict(base_log)
        panel_log_context.update(
            {
                "event": "panel_posted",
                "result": "posted" if posted_new_message else "refreshed",
                "source": self._sources.get(thread_id),
                "message_id": int(message.id),
                "custom_id": panels.OPEN_QUESTIONS_CUSTOM_ID,
                "view_timeout": view.timeout,
                "disable_on_timeout": getattr(view, "disable_on_timeout", None),
            }
        )
        await logs.send_welcome_log(
            "info" if posted_new_message else "debug",
            **panel_log_context,
        )
        if diag.is_enabled():
            parent_id = getattr(thread, "parent_id", None)
            try:
                parent_id_int = int(parent_id) if parent_id is not None else None
            except (TypeError, ValueError):
                parent_id_int = None
            target_user_id = self._target_users.get(thread_id)
            await diag.log_event(
                "info",
                "panel_posted",
                message_id=int(message.id),
                thread_id=thread_id,
                parent_id=parent_id_int,
                schema_id=session.schema_hash,
                custom_id=panels.OPEN_QUESTIONS_CUSTOM_ID,
                view_timeout=view.timeout,
                disable_on_timeout=getattr(view, "disable_on_timeout", None),
                target_user_id=target_user_id,
                ambiguous_target=target_user_id is None,
            )

        await self._persist_session_start(
            thread_id,
            panel_message_id=int(getattr(message, "id", 0)) if message else None,
            session_data=session,
            panel_created_at=getattr(message, "created_at", None),
        )

    async def _start_select_step(self, thread: discord.Thread, session: SessionData) -> None:
        thread_id = int(thread.id)
        async def gate(interaction: discord.Interaction) -> bool:
            allowed, _ = await self.check_interaction(thread_id, interaction)
            return allowed

        pending = session.pending_step or {}
        page = int(pending.get("page", 0))
        view = build_select_view(
            self._questions[thread_id],
            session.visibility,
            session.answers,
            interaction_check=gate,
            page=page,
        )
        if view is None:
            await self._show_preview(thread, session)
            return

        view.on_change = self._select_changed(thread_id)
        view.on_complete = self._select_completed(thread_id)
        view.on_page_change = self._select_page_updated(thread_id)
        store.set_pending_step(thread_id, {"kind": "select", "index": 0, "page": view.page})
        content = self._select_intro_text()
        message = self._select_messages.get(thread_id)
        if message:
            await message.edit(content=content, view=view)
        else:
            message = await thread.send(content, view=view)
            self._select_messages[thread_id] = message
        await logs.send_welcome_log(
            "debug",
            view="select",
            result="ready",
            **self._log_fields(thread_id),
        )

    def build_modal_stub(self, thread_id: int, *, index: Optional[int] = None) -> WelcomeQuestionnaireModal:
        session = store.get(thread_id)
        questions = self._questions.get(thread_id)
        answers = session.answers if session else {}
        visibility = session.visibility if session else {}
        pending = session.pending_step or {} if session else {}
        step_index = index if index is not None else int(pending.get("index", 0) or 0)

        modals = build_modals(
            questions or [],
            visibility,
            answers,
            title_prefix=self._modal_title_prefix(),
        )

        if modals:
            if step_index < 0:
                step_index = 0
            if step_index >= len(modals):
                step_index = len(modals) - 1
            modal = modals[step_index]
            modal.submit_callback = self._modal_submitted(thread_id, list(modal.questions), modal.step_index)
        else:
            modal = WelcomeQuestionnaireModal(
                questions=[],
                step_index=0,
                total_steps=1,
                title_prefix=self._modal_title_prefix(),
                answers=answers,
                visibility=visibility,
                on_submit=self._modal_submitted(thread_id, [], 0),
            )

        setattr(modal, "_c1c_thread_id", thread_id)
        setattr(modal, "_c1c_index", getattr(modal, "step_index", 0))
        return modal

    async def get_or_load_questions(
        self,
        thread_id: int,
        *,
        session: SessionData | None = None,
    ) -> list[Question] | None:
        questions = self._questions.get(thread_id)
        if questions:
            return questions
        restored = await self._rehydrate_questions(thread_id, session=session)
        if restored:
            return self._questions.get(thread_id)
        return None

    async def prompt_retry(self, interaction: discord.Interaction, thread_id: int) -> None:
        thread = self._threads.get(thread_id)
        if thread is None and isinstance(interaction.channel, discord.Thread):
            thread = interaction.channel
        if thread is None:
            return
        try:
            await thread.send("Something blinked. Tap **Open questions** again to continue.")
        except Exception:
            log.warning("failed to post retry prompt", exc_info=True)

    async def prompt_next(self, interaction: discord.Interaction, thread_id: int, next_idx: int) -> None:
        thread = self._threads.get(thread_id)
        if thread is None and isinstance(interaction.channel, discord.Thread):
            thread = interaction.channel
        if thread is None:
            return
        view = self.make_next_button(thread_id, next_idx)
        try:
            existing_id = self._next_prompt_message_ids.get(thread_id)
            if existing_id:
                try:
                    previous = await thread.fetch_message(existing_id)
                except Exception:
                    previous = None
                if previous is not None:
                    try:
                        await previous.delete()
                    except Exception:
                        log.debug("failed to delete previous next-step prompt", exc_info=True)
            message = await thread.send("Next up…", view=view)
            self._next_prompt_message_ids[thread_id] = int(message.id)
        except Exception:
            log.warning("failed to post next-step prompt", exc_info=True)

    def make_next_button(self, thread_id: int, next_idx: int) -> NextStepView:
        return NextStepView(self, thread_id, next_idx)

    async def render_inline_step(
        self,
        interaction: discord.Interaction,
        thread_id: int,
        *,
        index: int | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        diag_state = diag.interaction_state(interaction)
        diag_state["thread_id"] = thread_id
        diag_state["view"] = "inline"
        data = getattr(interaction, "data", None)
        if isinstance(data, dict):
            diag_state["custom_id"] = data.get("custom_id")
        target_user_id = self._target_users.get(thread_id)
        diag_state["target_user_id"] = target_user_id
        diag_state["ambiguous_target"] = target_user_id is None

        response = getattr(interaction, "response", None)
        followup = getattr(interaction, "followup", None)
        response_done = False
        if response is not None:
            is_done = getattr(response, "is_done", None)
            if callable(is_done):
                try:
                    response_done = bool(is_done())
                except Exception:
                    response_done = False
            elif isinstance(is_done, bool):
                response_done = is_done
        if response_done:
            diag_state["response_is_done"] = True

        session = store.get(thread_id)
        if session is None:
            session = store.ensure(thread_id, flow=self.flow, schema_hash=schema_hash(self.flow))
        if session is not None:
            session.status = "in_progress"
            session.thread_id = thread_id

        try:
            questions_for_thread = await self.get_or_load_questions(thread_id, session=session)
        except Exception:
            log.warning("failed to load questions for inline wizard", exc_info=True)
            raise

        if not questions_for_thread:
            store.set_pending_step(thread_id, None)
            if diag.is_enabled():
                await diag.log_event(
                    "warning",
                    "inline_launch_skipped",
                    skip_reason="no_questions",
                    **diag_state,
                )
            return

        if not session.visibility:
            try:
                session.visibility = rules.evaluate_visibility(
                    questions_for_thread,
                    session.answers,
                )
            except Exception:
                session.visibility = {}

        pending = session.pending_step or {}
        if index is None:
            raw_index = pending.get("index", 0) if isinstance(pending, dict) else 0
            try:
                index = int(raw_index)
            except (TypeError, ValueError):
                index = 0
        total_questions = len(questions_for_thread)
        if total_questions <= 0:
            raise RuntimeError("no inline steps available")
        resolved_index, _ = self.resolve_step(thread_id, index, direction=1)
        if resolved_index is None:
            store.set_pending_step(thread_id, None)
            content = "All onboarding questions are complete."
            wizard: panels.OnboardWizard | None = None
            if session is not None:
                session.current_question_index = None
        else:
            index = resolved_index
            self._set_current_step_for_thread(thread_id, index)
            try:
                content = self.build_panel_content(thread_id, index)
            except Exception:
                log.warning("failed to render inline step", exc_info=True)
                raise
            wizard = panels.OnboardWizard(self, thread_id, step=index)
            if session is not None:
                session.current_question_index = index
                session.status = "in_progress"

        if wizard is not None:
            try:
                content = wizard._apply_requirement_suffix(content, wizard._question())
            except Exception:
                log.debug("failed to apply requirement suffix", exc_info=True)

        diag_state["step_index"] = index
        diag_state["total_steps"] = total_questions

        message: discord.Message | None = None
        reused_existing = False
        existing_map = getattr(self, "_inline_messages", None)
        id_map = getattr(self, "_inline_message_ids", None)

        thread: discord.Thread | None = self._threads.get(thread_id)
        if thread is None and isinstance(interaction.channel, discord.Thread):
            thread = interaction.channel

        if isinstance(existing_map, dict):
            existing_message = existing_map.get(thread_id)
            if existing_message is not None:
                try:
                    await existing_message.edit(content=content, view=wizard)
                except Exception:
                    existing_map.pop(thread_id, None)
                else:
                    message = existing_message
                    reused_existing = True
                    if isinstance(id_map, dict):
                        try:
                            id_map[thread_id] = int(existing_message.id)
                        except Exception:
                            id_map.pop(thread_id, None)

        if message is None and isinstance(id_map, dict):
            message_id = id_map.get(thread_id)
            if message_id and thread is not None:
                fetched: discord.Message | None = None
                try:
                    fetched = await thread.fetch_message(message_id)
                except Exception:
                    id_map.pop(thread_id, None)
                if fetched is not None:
                    try:
                        await fetched.edit(content=content, view=wizard)
                    except Exception:
                        id_map.pop(thread_id, None)
                        try:
                            await fetched.delete()
                        except Exception:
                            log.debug("failed to delete stale inline wizard message", exc_info=True)
                    else:
                        message = fetched
                        reused_existing = True
                        if isinstance(existing_map, dict):
                            existing_map[thread_id] = fetched
                        try:
                            id_map[thread_id] = int(fetched.id)
                        except Exception:
                            id_map.pop(thread_id, None)

        send_callable: Callable[..., Awaitable[Any]] | None = None
        uses_followup = False
        if message is None:
            if response_done:
                send_callable = getattr(followup, "send", None)
                uses_followup = True
            else:
                send_callable = getattr(response, "send_message", None)

            if not callable(send_callable):
                if diag.is_enabled():
                    await diag.log_event(
                        "warning",
                        "inline_launch_skipped",
                        skip_reason="no_send_callable",
                        **diag_state,
                    )
                return

            try:
                if uses_followup:
                    message = await send_callable(content=content, view=wizard)  # type: ignore[misc]
                else:
                    await send_callable(content=content, view=wizard)
            except Exception as exc:
                if diag.is_enabled():
                    await diag.log_event(
                        "warning",
                        "inline_launch_failed",
                        exception_type=exc.__class__.__name__,
                        exception_message=str(exc),
                        **diag_state,
                    )
                raise
            else:
                if not uses_followup:
                    try:
                        message = await interaction.original_response()
                    except Exception:
                        message = None

        if not reused_existing and message is not None and isinstance(id_map, dict):
            try:
                id_map[thread_id] = int(message.id)
            except Exception:
                id_map.pop(thread_id, None)

        if reused_existing and response is not None and not response_done:
            try:
                await response.defer(ephemeral=True)
            except Exception:
                pass

        if diag.is_enabled():
            await diag.log_event("info", "inline_wizard_posted", **diag_state)

        if message is not None:
            wizard.attach(message)
            if isinstance(existing_map, dict):
                existing_map[thread_id] = message
            prompt_id = self._next_prompt_message_ids.get(thread_id)
            prompt_message = getattr(interaction, "message", None)
            if prompt_id and isinstance(prompt_message, discord.Message):
                try:
                    if int(prompt_message.id) == prompt_id:
                        try:
                            await prompt_message.delete()
                        except Exception:
                            log.debug("failed to delete next-step prompt message", exc_info=True)
                        finally:
                            self._next_prompt_message_ids.pop(thread_id, None)
                except Exception:
                    log.debug("failed to inspect next-step prompt message", exc_info=True)

        log_payload = self._log_fields(thread_id, actor=getattr(interaction, "user", None))
        if context is not None:
            log_payload.update(context)
        log_payload.setdefault("source", self._sources.get(thread_id, "unknown"))
        await logs.send_welcome_log(
            "debug",
            view="inline",
            result="launched",
            index=index,
            **log_payload,
        )

    def _record_captured_message(self, thread_id: int, message: discord.Message) -> None:
        captured = getattr(self, "_captured_msgs", None)
        if not isinstance(captured, dict):
            captured = {}
            setattr(self, "_captured_msgs", captured)
        try:
            message_id = int(getattr(message, "id", 0) or 0)
        except (TypeError, ValueError):
            return
        if message_id <= 0:
            return
        captured.setdefault(thread_id, []).append(message_id)

    async def _react_to_message(self, message: discord.Message, emoji: str) -> None:
        add_reaction = getattr(message, "add_reaction", None)
        if not callable(add_reaction):
            return
        try:
            await add_reaction(emoji)
        except Exception:
            log.debug("failed to add reaction", exc_info=True)

    async def _refresh_inline_message(self, thread_id: int, *, index: int) -> None:
        questions = self._questions.get(thread_id) or []
        if not questions:
            return
        try:
            content = self.build_panel_content(thread_id, index)
        except Exception:
            log.debug("failed to render inline content during refresh", exc_info=True)
            return

        wizard = panels.OnboardWizard(self, thread_id, step=index)
        try:
            content = wizard._apply_requirement_suffix(content, wizard._question())
        except Exception:
            pass

        resolved_thread: discord.Thread | None = self._threads.get(thread_id)
        if resolved_thread is None:
            session = store.get(thread_id)
            candidate_thread_id: int | None = None
            if session is not None:
                try:
                    candidate_thread_id = int(session.thread_id) if session.thread_id is not None else None
                except (TypeError, ValueError):
                    candidate_thread_id = None
            if candidate_thread_id is None:
                candidate_thread_id = thread_id
            try:
                channel = self.bot.get_channel(candidate_thread_id)
            except Exception:
                channel = None
            if isinstance(channel, discord.Thread):
                resolved_thread = channel
                self._threads[thread_id] = channel

        message_obj: discord.Message | None = None
        inline_map = getattr(self, "_inline_messages", None)
        if isinstance(inline_map, dict):
            message_obj = inline_map.get(thread_id)

        if message_obj is None:
            id_map = getattr(self, "_inline_message_ids", None)
            message_id = id_map.get(thread_id) if isinstance(id_map, dict) else None
            fetcher = getattr(resolved_thread, "fetch_message", None) if resolved_thread is not None else None
            if callable(fetcher) and message_id:
                try:
                    message_obj = await fetcher(message_id)
                except Exception:
                    message_obj = None
                    if isinstance(id_map, dict):
                        id_map.pop(thread_id, None)

        if message_obj is not None:
            try:
                await message_obj.edit(content=content, view=wizard)
            except Exception:
                log.debug("failed to refresh inline wizard message", exc_info=True)
                return
        else:
            if resolved_thread is None:
                log.debug(
                    "welcome.inline_refresh.thread_missing",
                    extra={"thread": thread_id, "index": index},
                )
                return
            try:
                message_obj = await resolved_thread.send(content=content, view=wizard)
            except Exception:
                log.debug("failed to re-render inline wizard message", exc_info=True)
                return

        try:
            wizard.attach(message_obj)
        except Exception:
            pass

        if isinstance(inline_map, dict):
            inline_map[thread_id] = message_obj
        id_map = getattr(self, "_inline_message_ids", None)
        if isinstance(id_map, dict):
            try:
                id_map[thread_id] = int(getattr(message_obj, "id", 0))
            except Exception:
                pass

    def _resolve_inline_index(self, thread_id: int, session: SessionData) -> tuple[int | None, str]:
        questions = self._questions.get(thread_id) or []
        pending = session.pending_step or {}
        pending_kind = pending.get("kind") if isinstance(pending, dict) else None

        def _valid_inline(idx: int | None) -> bool:
            if idx is None or not (0 <= idx < len(questions)):
                return False
            qtype = self._question_type_value(questions[idx]).strip().lower()
            return qtype in TEXT_TYPES

        pending_index: int | None = None
        try:
            pending_index = int(pending.get("index", 0)) if isinstance(pending, dict) else None
        except (TypeError, ValueError):
            pending_index = None

        if _valid_inline(pending_index):
            if pending_kind != "inline":
                store.set_pending_step(thread_id, {"kind": "inline", "index": cast(int, pending_index)})
                return cast(int, pending_index), "pending_rehydrated"
            return cast(int, pending_index), "pending"

        current_index: int | None = None
        try:
            current_index = (
                int(session.current_question_index)
                if session.current_question_index is not None
                else None
            )
        except (TypeError, ValueError):
            current_index = None

        if _valid_inline(current_index):
            store.set_pending_step(thread_id, {"kind": "inline", "index": cast(int, current_index)})
            return cast(int, current_index), "current_index"

        return None, pending_kind or "unknown"

    async def handle_thread_message(self, message: discord.Message) -> bool:
        channel = getattr(message, "channel", None)
        thread_identifier = getattr(channel, "id", None)
        try:
            thread_id = int(thread_identifier)
        except (TypeError, ValueError):
            return False

        if thread_id not in self._questions:
            return False

        author = getattr(message, "author", None)
        if author is None or getattr(author, "bot", False):
            return False

        session = store.get(thread_id)
        if session is None:
            return False

        author_identifier = getattr(author, "id", None)
        try:
            author_id = int(author_identifier)
        except (TypeError, ValueError):
            return False

        if session.respondent_id is None:
            session.respondent_id = author_id
            session.touch()
        elif session.respondent_id != author_id:
            log.debug(
                "welcome.auto_capture.ignore_other_author",
                extra={
                    "thread": thread_id,
                    "author": author_id,
                    "respondent": session.respondent_id,
                    "status": session.status,
                },
            )
            return False

        if session.status == "completed":
            return False

        index, capture_mode = self._resolve_inline_index(thread_id, session)
        if index is None:
            log.debug(
                "welcome.auto_capture.no_inline_step",
                extra={
                    "thread": thread_id,
                    "author": author_id,
                    "pending": session.pending_step,
                    "current": session.current_question_index,
                    "status": session.status,
                },
            )
            return False

        questions = self._questions.get(thread_id) or []
        question = questions[index]
        qtype = self._question_type_value(question).strip().lower()
        if qtype not in TEXT_TYPES:
            return False

        content = (message.content or "").strip()
        if not content:
            return False

        meta = self._question_meta(question)
        ok, cleaned, error = self.validate_answer(meta, content)
        if not ok:
            await self._react_to_message(message, "❌")
            return True

        value = cleaned if cleaned is not None else content
        await self.set_answer(thread_id, self._question_key(question), value)

        session.respondent_id = session.respondent_id or author_id
        session.status = "in_progress"
        session.current_question_index = index
        session.touch()

        self._record_captured_message(thread_id, message)
        await self._react_to_message(message, "✅")
        await self._refresh_inline_message(thread_id, index=index)
        await _delete_captured_answer_message(message)
        try:
            qkey = self._question_key(question)
        except Exception:
            qkey = "unknown"
        log.info(
            "onboarding_fallback_answer_captured — thread=%s • question=%s • len=%d • mode=%s",
            thread_id,
            qkey,
            len(content),
            capture_mode,
        )
        return True

    async def start_session_from_button(
        self,
        thread_id: int,
        *,
        actor_id: int | None = None,
        channel: discord.abc.Messageable | None = None,
        guild: discord.Guild | None = None,
        interaction: discord.Interaction,
    ) -> None:
        context = {
            "view": "panel",
            "view_tag": panels.WELCOME_PANEL_TAG,
            "custom_id": panels.OPEN_QUESTIONS_CUSTOM_ID,
        }
        allowed, _ = await self.check_interaction(thread_id, interaction, context=context)
        if not allowed:
            return

        session = store.ensure(thread_id, flow=self.flow, schema_hash=schema_hash(self.flow))
        session.status = "in_progress"
        session.thread_id = thread_id

        if session.respondent_id is None:
            user_identifier = getattr(getattr(interaction, "user", None), "id", None)
            try:
                session.respondent_id = int(user_identifier) if user_identifier is not None else None
            except (TypeError, ValueError):
                session.respondent_id = session.respondent_id

        if session.respondent_id is None and actor_id is not None:
            try:
                session.respondent_id = int(actor_id)
            except (TypeError, ValueError):
                session.respondent_id = session.respondent_id
        session.touch()

        try:
            await self.render_inline_step(
                interaction,
                thread_id,
                context={"source": self._sources.get(thread_id, "panel")},
            )
        except Exception:
            log.warning("failed to launch inline onboarding wizard", exc_info=True)
            raise

    async def finish_onboarding(
        self,
        interaction: discord.Interaction,
        thread_id: int,
        *,
        session: SessionData,
        thread: discord.Thread,
    ) -> None:
        await self._start_select_step(thread, session)
        refreshed = store.get(thread_id)
        pending = refreshed.pending_step if refreshed is not None else None
        if not pending:
            try:
                await thread.send("All set. A recruiter will review your answers shortly.")
            except Exception:
                log.warning("failed to send onboarding completion notice", exc_info=True)

    def _select_changed(self, thread_id: int) -> Callable[[discord.Interaction, Question, list[str]], Awaitable[None]]:
        async def handler(
            interaction: discord.Interaction,
            question: Question,
            values: list[str],
        ) -> None:
            allowed, _ = await self.check_interaction(thread_id, interaction)
            if not allowed:
                return
            await self._handle_select_change(thread_id, interaction, question, values)

        return handler

    def _select_completed(self, thread_id: int) -> Callable[[discord.Interaction], Awaitable[None]]:
        async def handler(interaction: discord.Interaction) -> None:
            allowed, _ = await self.check_interaction(thread_id, interaction)
            if not allowed:
                return
            await self._handle_select_complete(thread_id, interaction)

        return handler

    async def _handle_modal_launch(
        self,
        thread_id: int,
        interaction: discord.Interaction,
        *,
        context: dict[str, Any] | None = None,
    ) -> None:
        await self.render_inline_step(interaction, thread_id, context=context)

    async def _rehydrate_questions(
        self,
        thread_id: int,
        *,
        session: SessionData | None,
    ) -> bool:
        """Best-effort attempt to restore question state for a thread."""

        questions = self._questions.get(thread_id)
        if questions:
            return True

        try:
            from shared.sheets import onboarding_questions
        except Exception:
            return False

        try:
            refreshed = onboarding_questions.get_questions(self.flow)
        except Exception:
            log.warning(
                "failed to rehydrate welcome questions", exc_info=True
            )
            return False

        self._questions[thread_id] = list(refreshed)
        if session is not None:
            try:
                session.visibility = rules.evaluate_visibility(
                    self._questions[thread_id],
                    session.answers,
                )
            except Exception:
                log.warning(
                    "failed to recompute visibility during welcome rehydrate",
                    exc_info=True,
                )
        return True

    async def _restart_from_interaction(
        self,
        thread_id: int,
        interaction: discord.Interaction,
        *,
        context: dict[str, Any] | None = None,
    ) -> None:
        channel = interaction.channel if isinstance(interaction.channel, discord.Thread) else None
        thread = self._threads.get(thread_id) or channel
        message = getattr(interaction, "message", None)
        message_id = getattr(message, "id", None)

        log_payload = self._log_fields(thread_id, actor=interaction.user)
        if context is not None:
            log_payload.update(context)
        if thread is not None:
            log_payload.setdefault("thread", logs.format_thread(getattr(thread, "id", None)))
            parent_id = getattr(thread, "parent_id", None)
            if parent_id is not None:
                try:
                    log_payload.setdefault("parent_channel_id", int(parent_id))
                except (TypeError, ValueError):
                    pass
        if message_id is not None:
            try:
                log_payload.setdefault("message_id", int(message_id))
            except (TypeError, ValueError):
                pass
        log_payload["view"] = "panel"
        log_payload.setdefault("view_tag", panels.WELCOME_PANEL_TAG)
        log_payload.setdefault("custom_id", panels.OPEN_QUESTIONS_CUSTOM_ID)
        log_payload["result"] = "restarted"

        try:
            await interaction.response.defer(ephemeral=True)
        except discord.InteractionResponded:
            pass
        except Exception:
            log.warning("failed to defer restart notice", exc_info=True)

        await logs.send_welcome_log("info", **log_payload)

        self._cleanup_session(thread_id)

        if thread is None:
            failure = dict(log_payload)
            failure["result"] = "error"
            failure["reason"] = "thread_missing"
            await logs.send_welcome_log("error", **failure)
            return

        try:
            from modules.onboarding.welcome_flow import start_welcome_dialog

            panel_message = message if isinstance(message, discord.Message) else None
            panel_id = None
            if message_id is not None:
                try:
                    panel_id = int(message_id)
                except (TypeError, ValueError):
                    panel_id = None
            await start_welcome_dialog(
                thread,
                interaction.user,
                "panel_restart",
                bot=self.bot,
                panel_message_id=panel_id,
                panel_message=panel_message,
            )
        except Exception as exc:  # pragma: no cover - network restart errors
            error_payload = dict(log_payload)
            error_payload["result"] = "error"
            await logs.send_welcome_exception("error", exc, **error_payload)

    def _modal_submitted(
        self,
        thread_id: int,
        questions: Sequence[Question],
        index: int,
    ) -> Callable[[discord.Interaction, dict[str, str]], Awaitable[None]]:
        async def handler(
            interaction: discord.Interaction,
            values: dict[str, str],
        ) -> None:
            await self._handle_modal_submit(thread_id, interaction, questions, index, values)

        return handler

    async def _handle_modal_submit(
        self,
        thread_id: int,
        interaction: discord.Interaction,
        questions: Sequence[Question],
        index: int,
        values: dict[str, str],
    ) -> None:
        session = store.get(thread_id)
        thread = self._threads.get(thread_id)
        await interaction.response.defer(ephemeral=True)

        if session is None or thread is None:
            await logs.send_welcome_log(
                "warn",
                view="modal",
                result="inactive",
                index=index,
                **self._log_fields(thread_id, actor=interaction.user),
            )
            await _safe_ephemeral(interaction, "⚠️ This onboarding session is no longer active.")
            return

        questions_for_thread = await self.get_or_load_questions(thread_id, session=session)
        if not questions_for_thread:
            await logs.send_welcome_log(
                "warning",
                view="modal",
                result="questions_missing",
                index=index,
                **self._log_fields(thread_id, actor=interaction.user),
            )
            await _safe_ephemeral(
                interaction,
                "⚠️ Something blinked. Tap **Open questions** again to continue.",
            )
            await self.prompt_retry(interaction, thread_id)
            return

        if not session.visibility:
            try:
                session.visibility = rules.evaluate_visibility(
                    questions_for_thread,
                    session.answers,
                )
            except Exception:
                session.visibility = {}

        gate_context: dict[str, Any] = {
            "view": "modal",
            "view_tag": panels.WELCOME_PANEL_TAG,
            "custom_id": panels.OPEN_QUESTIONS_CUSTOM_ID,
            "view_id": panels.OPEN_QUESTIONS_CUSTOM_ID,
            "step_index": index,
            "flow": self.flow,
        }
        gate_context.setdefault("diag", f"{self.flow}_flow")
        schema_id = getattr(session, "schema_hash", None)
        if schema_id is not None:
            try:
                gate_context["schema_id"] = int(schema_id)
            except (TypeError, ValueError):
                gate_context["schema_id"] = schema_id

        allowed, _ = await self.check_interaction(
            thread_id,
            interaction,
            context=gate_context,
        )
        if not allowed:
            return

        modals_before = build_modals(
            questions_for_thread,
            session.visibility,
            session.answers,
            title_prefix=self._modal_title_prefix(),
        )
        total_before = len(modals_before)

        if total_before and index >= total_before:
            store.set_pending_step(thread_id, {"kind": "inline", "index": 0})
            await logs.send_welcome_log(
                "debug",
                view="modal",
                result="oob_reset",
                index=index,
                **self._log_fields(thread_id, actor=interaction.user),
            )
            await _safe_ephemeral(
                interaction,
                "⚠️ Let’s restart that section. Tap **Open questions** again in the thread.",
            )
            await self.prompt_retry(interaction, thread_id)
            return

        for question in questions:
            raw_value = values.get(question.qid, "")
            state = _visible_state(session.visibility, question.qid)
            required = _is_effectively_required(question, session.visibility)
            answer = raw_value.strip()
            if required and not answer:
                await _safe_ephemeral(
                    interaction,
                    f"⚠️ **{question.label}** is required.",
                )
                return

            meta = self._question_meta(question)

            if not answer:
                await self.set_answer(thread_id, question.qid, None)
                continue

            ok, cleaned, err = self.validate_answer(meta, answer)
            if not ok:
                await self._send_validation_error(interaction, thread_id, meta, err)
                return

            if diag.is_enabled():
                await diag.log_event(
                    "info",
                    "welcome_validator_branch",
                    qid=meta.get("qid"),
                    have_regex=self._has_sheet_regex(meta),
                    type=meta.get("type"),
                )

            await self.set_answer(thread_id, question.qid, cleaned)

        session.visibility = rules.evaluate_visibility(
            questions_for_thread,
            session.answers,
        )

        modals_after = build_modals(
            questions_for_thread,
            session.visibility,
            session.answers,
            title_prefix=self._modal_title_prefix(),
        )
        total_after = len(modals_after)

        await logs.send_welcome_log(
            "debug",
            view="modal",
            result="saved",
            index=index,
            **self._log_fields(thread_id, actor=interaction.user),
        )

        retry_registry = getattr(self, "retry_message_ids", None)
        if isinstance(retry_registry, dict):
            retry_message_id = retry_registry.pop(thread_id, None)
            if retry_message_id:
                try:
                    message = await thread.fetch_message(retry_message_id)
                    await message.delete()
                except Exception:  # pragma: no cover - best-effort cleanup
                    pass

        display_name = _display_name(getattr(interaction, "user", None))
        channel_obj: discord.abc.GuildChannel | discord.Thread | None
        channel_obj = interaction.channel if isinstance(interaction.channel, (discord.Thread, discord.abc.GuildChannel)) else thread
        log.info(
            "✅ Welcome — modal_submit_ok • user=%s • channel=%s",
            display_name,
            _channel_path(channel_obj),
        )

        next_index = index + 1 if index + 1 < total_after else None
        if next_index is None:
            store.set_pending_step(thread_id, None)
            await _safe_ephemeral(
                interaction,
                "✅ Saved! You’re all set for this section.",
            )
            await self.finish_onboarding(
                interaction,
                thread_id,
                session=session,
                thread=thread,
            )
            return

        store.set_pending_step(thread_id, {"kind": "inline", "index": next_index})
        await _safe_ephemeral(
            interaction,
            "✅ Saved! Opening the next question…",
        )
        try:
            await self.render_inline_step(
                interaction,
                thread_id,
                index=next_index,
                context={"source": self._sources.get(thread_id, "modal")},
            )
        except Exception:
            log.warning(
                "failed to render inline step after modal submission", exc_info=True
            )
            await self.prompt_next(interaction, thread_id, next_index)

    async def _handle_select_change(
        self,
        thread_id: int,
        interaction: discord.Interaction,
        question: Question,
        values: list[str],
    ) -> None:
        session = store.get(thread_id)
        thread = self._threads.get(thread_id)
        if session is None or thread is None:
            await logs.send_welcome_log(
                "warn",
                view="select",
                result="inactive",
                question=question.qid,
                **self._log_fields(thread_id, actor=interaction.user),
            )
            await _safe_ephemeral(interaction, "⚠️ This onboarding session expired.")
            return

        self._store_select_answer(session, question, values)
        session.visibility = rules.evaluate_visibility(
            self._questions[thread_id],
            session.answers,
        )
        store.set_pending_step(thread_id, session.pending_step)

        async def gate(interaction: discord.Interaction) -> bool:
            allowed, _ = await self.check_interaction(thread_id, interaction)
            return allowed

        pending = session.pending_step or {}
        page = int(pending.get("page", 0))
        view = build_select_view(
            self._questions[thread_id],
            session.visibility,
            session.answers,
            interaction_check=gate,
            page=page,
        )
        if view is None:
            store.set_pending_step(thread_id, None)
            await interaction.response.edit_message(view=None)
            await logs.send_welcome_log(
                "debug",
                view="select",
                result="completed",
                question=question.qid,
                **self._log_fields(thread_id, actor=interaction.user),
            )
            await self._show_preview(thread, session)
            return

        view.on_change = self._select_changed(thread_id)
        view.on_complete = self._select_completed(thread_id)
        view.on_page_change = self._select_page_updated(thread_id)
        store.set_pending_step(thread_id, {"kind": "select", "index": 0, "page": view.page})
        await interaction.response.edit_message(view=view)
        await logs.send_welcome_log(
            "debug",
            view="select",
            result="changed",
            question=question.qid,
            **self._log_fields(thread_id, actor=interaction.user),
        )

    async def _handle_select_complete(
        self,
        thread_id: int,
        interaction: discord.Interaction,
    ) -> None:
        session = store.get(thread_id)
        thread = self._threads.get(thread_id)
        if session is None or thread is None:
            await logs.send_welcome_log(
                "warn",
                view="select",
                result="inactive",
                **self._log_fields(thread_id, actor=interaction.user),
            )
            await _safe_ephemeral(interaction, "⚠️ This onboarding session expired.")
            return

        missing = _missing_required_selects(
            self._questions[thread_id],
            session.visibility,
            session.answers,
        )
        if missing:
            labels = ", ".join(f"**{label}**" for label in missing)
            await _safe_ephemeral(
                interaction,
                f"⚠️ Please choose at least one option for {labels}.",
            )
            return

        store.set_pending_step(thread_id, None)
        await interaction.response.edit_message(view=None)
        await logs.send_welcome_log(
            "info",
            view="select",
            result="submitted",
            **self._log_fields(thread_id, actor=interaction.user),
        )
        await self._show_preview(thread, session)

    def _select_page_updated(self, thread_id: int) -> Callable[[discord.Interaction, int], Awaitable[None]]:
        async def handler(interaction: discord.Interaction, page: int) -> None:
            allowed, _ = await self.check_interaction(thread_id, interaction)
            if not allowed:
                return
            session = store.get(thread_id)
            if session is None:
                return
            pending = dict(session.pending_step or {"kind": "select", "index": 0})
            pending["page"] = page
            store.set_pending_step(thread_id, pending)

        return handler

    async def _show_preview(
        self,
        thread: discord.Thread,
        session: SessionData,
    ) -> None:
        thread_id = int(thread.id)
        embed = self._build_preview_embed(thread_id, session)
        view = _PreviewView(self, thread_id)
        message = self._preview_messages.get(thread_id)
        if message:
            await message.edit(embed=embed, view=view)
        else:
            message = await thread.send(embed=embed, view=view)
            self._preview_messages[thread_id] = message
        store.set_preview_message(
            thread_id,
            message_id=message.id,
            channel_id=getattr(message.channel, "id", None),
        )
        store.set_pending_step(thread_id, None)
        if thread_id not in self._preview_logged:
            self._preview_logged.add(thread_id)
            await logs.send_welcome_log(
                "debug",
                view="preview",
                result="rendered",
                source=self._sources.get(thread_id, "unknown"),
                schema=session.schema_hash,
                **self._log_fields(thread_id),
            )

    def _build_preview_embed(self, thread_id: int, session: SessionData) -> discord.Embed:
        embed = discord.Embed(title="Review your responses")
        embed.description = "Confirm the information below before submitting."

        for question in self._questions.get(thread_id, []):
            state = _visible_state(session.visibility, question.qid)
            if state == "skip":
                continue
            value = _preview_value_for_question(question, session.answers.get(question.qid))
            if not value:
                value = "*(skipped)*"
            embed.add_field(name=question.label, value=value, inline=False)
        return embed

    async def _handle_confirm(
        self,
        thread_id: int,
        interaction: discord.Interaction,
    ) -> None:
        session = store.get(thread_id)
        thread = self._threads.get(thread_id)
        if session is None or thread is None:
            await logs.send_welcome_log(
                "warn",
                view="preview",
                result="inactive",
                **self._log_fields(thread_id, actor=interaction.user),
            )
            await _safe_ephemeral(interaction, "⚠️ This onboarding session expired.")
            return

        questions_for_thread = self._questions.get(thread_id, [])
        visibility_map = session.visibility or {}
        if not visibility_map and questions_for_thread:
            try:
                visibility_map = rules.evaluate_visibility(
                    questions_for_thread,
                    session.answers,
                )
            except Exception:
                visibility_map = {}
            else:
                session.visibility = visibility_map

        missing_required = submit.missing_required_questions(
            questions_for_thread,
            visibility_map,
            session.answers,
        )
        if missing_required:
            labels = ", ".join(f"**{question.label}**" for question in missing_required)
            await _safe_ephemeral(
                interaction,
                f"You missed: {labels}.",
            )
            return

        await interaction.response.edit_message(view=None)

        preview_message = self._preview_messages.pop(thread_id, None)
        if preview_message is not None:
            try:
                await preview_message.delete()
            except Exception:
                try:
                    await preview_message.edit(view=None)
                except Exception:
                    log.warning("failed to remove preview after confirmation", exc_info=True)

        summary_author = self._resolve_summary_author(thread, interaction)
        summary_success = await self._send_welcome_summary_safe(
            thread=thread,
            answers=dict(session.answers),
            author=summary_author,
            schema_hash=session.schema_hash or "",
            visibility=session.visibility,
        )

        if summary_success:
            await thread.send("@RecruitmentCoordinator")

            await self._log_panel_completion(
                thread_id,
                thread=thread,
                actor=interaction.user,
                session=session,
                answers=dict(session.answers),
            )

            await self._persist_session_completion(
                thread_id,
                session_data=session,
                answers=dict(session.answers),
                panel_message_id=self._panel_messages.get(thread_id),
            )

        store.set_preview_message(thread_id, message_id=None, channel_id=None)
        self._cleanup_session(
            thread_id,
            preserve_session=bool(summary_embed and _is_fallback_summary(summary_embed)),
        )

    async def _handle_edit(
        self,
        thread_id: int,
        interaction: discord.Interaction,
    ) -> None:
        session = store.get(thread_id)
        thread = self._threads.get(thread_id)
        if session is None or thread is None:
            await logs.send_welcome_log(
                "warn",
                view="preview",
                result="inactive",
                **self._log_fields(thread_id, actor=interaction.user),
            )
            await _safe_ephemeral(interaction, "⚠️ This onboarding session expired.")
            return

        await interaction.response.edit_message(view=None)
        await _safe_followup(
            interaction,
            "🛠️ Re-opening the form so you can make changes.",
        )
        await logs.send_welcome_log(
            "info",
            view="preview",
            result="reopened",
            **self._log_fields(thread_id, actor=interaction.user),
        )
        anchor = getattr(interaction, "message", None)
        await self._start_modal_step(thread, session, anchor=anchor)

    def _cleanup_session(self, thread_id: int, *, preserve_session: bool = False) -> None:
        if not preserve_session:
            store.end(thread_id)
        self._threads.pop(thread_id, None)
        self._questions.pop(thread_id, None)
        self.answers_by_thread.pop(thread_id, None)
        self._select_messages.pop(thread_id, None)
        self._preview_messages.pop(thread_id, None)
        self._sources.pop(thread_id, None)
        self._allowed_users.pop(thread_id, None)
        self._target_users.pop(thread_id, None)
        self._target_message_ids.pop(thread_id, None)
        self._preview_logged.discard(thread_id)
        panel_message_id = self._panel_messages.pop(thread_id, None)
        if panel_message_id is not None:
            panels.mark_panel_inactive_by_message(panel_message_id)
        self._prefetched_panels.pop(thread_id, None)
        self._inline_message_ids.pop(thread_id, None)
        panels.unbind_controller(thread_id)

    async def _ensure_target_cached(
        self, thread_id: int, *, refresh_if_none: bool = False
    ) -> None:
        cached = self._target_users.get(thread_id, ...)
        if cached is not ...:
            if not (cached is None and refresh_if_none):
                return

        thread = self._threads.get(thread_id)
        if thread is None:
            self._target_users[thread_id] = None
            return

        target_id: int | None = None
        message_id: int | None = None

        message = await locate_welcome_message(thread)
        target_id, message_id = extract_target_from_message(message)

        self._target_users[thread_id] = target_id
        if message_id is not None:
            self._target_message_ids[thread_id] = message_id

    @staticmethod
    def _member_role_ids(member: discord.abc.User | discord.Member | None) -> set[int]:
        if not isinstance(member, discord.Member):
            return set()
        role_ids: set[int] = set()
        for role in getattr(member, "roles", []) or []:
            role_id = getattr(role, "id", None)
            if role_id is None:
                continue
            try:
                role_ids.add(int(role_id))
            except (TypeError, ValueError):
                continue
        return role_ids

    async def _log_access(
        self,
        level: str,
        result: str,
        thread_id: int,
        interaction: discord.Interaction,
        context: dict[str, Any],
        **extra: Any,
    ) -> None:
        payload = self._log_fields(thread_id, actor=interaction.user)
        payload.update(context)
        payload["result"] = result
        if extra:
            payload.update(extra)
        await logs.send_welcome_log(level, **payload)

    async def check_interaction(
        self,
        thread_id: int,
        interaction: discord.Interaction,
        *,
        context: dict[str, Any] | None = None,
    ) -> tuple[bool, str | None]:
        allowed_cache = self._allowed_users.setdefault(thread_id, set())
        actor = interaction.user
        actor_id = getattr(actor, "id", None)

        log_context: dict[str, Any] | None = None
        if context is not None:
            log_context = dict(context)
            if actor_id is not None:
                log_context.setdefault("actor_id", int(actor_id))
            target_message_id = self._target_message_ids.get(thread_id)
            if target_message_id is not None:
                log_context.setdefault("target_message_id", int(target_message_id))
            thread = self._thread_for(thread_id)
            parent_id = getattr(thread, "parent_id", None)
            if parent_id is not None:
                log_context.setdefault("parent_channel_id", int(parent_id))
        target_id = self._target_users.get(thread_id)
        if target_id is not None and log_context is not None:
            log_context.setdefault("target_user_id", int(target_id))

        if actor_id is not None and actor_id in allowed_cache:
            fallback_thread = interaction.channel if isinstance(interaction.channel, discord.Thread) else self._thread_for(thread_id)
            _log_gate(
                interaction,
                allowed=True,
                reason="cached",
                fallback_thread=fallback_thread,
                flow=self.flow,
            )
            if log_context is not None:
                await self._log_access("info", "allowed", thread_id, interaction, log_context)
            return True, None

        thread = self._thread_for(thread_id)
        subject = interaction.channel if isinstance(interaction.channel, discord.Thread) else None

        if subject is None:
            _log_gate(
                interaction,
                allowed=False,
                reason="no_thread",
                fallback_thread=thread,
                flow=self.flow,
            )
            if log_context is not None:
                await self._log_access("warn", "denied_scope", thread_id, interaction, log_context)
            await _safe_ephemeral(
                interaction,
                "⚠️ This onboarding panel only works inside ticket threads.",
            )
            return False, "no_thread"

        perms = subject.permissions_for(cast(discord.abc.Snowflake, actor))
        if not perms.view_channel:
            _log_gate(
                interaction,
                allowed=False,
                reason="no_view_channel",
                fallback_thread=subject,
                flow=self.flow,
            )
            if log_context is not None:
                await self._log_access(
                    "warn",
                    "denied_permission",
                    thread_id,
                    interaction,
                    log_context,
                    reason="no_view_channel",
                )
            await _safe_ephemeral(
                interaction,
                "⚠️ You need view access to this thread before starting the onboarding form.",
            )
            return False, "no_view_channel"

        if actor_id is not None:
            allowed_cache.add(int(actor_id))
        if log_context is not None:
            await self._log_access("info", "allowed", thread_id, interaction, log_context)
        _log_gate(
            interaction,
            allowed=True,
            reason="view_channel",
            fallback_thread=subject,
            flow=self.flow,
        )
        return True, None

    def _store_select_answer(
        self,
        session: SessionData,
        question: Question,
        values: Iterable[str],
    ) -> None:
        option_objects = self._question_options(question)
        options: dict[str, dict[str, str]] = {}
        for option in option_objects:
            if isinstance(option, dict):
                label = str(option.get("label") or option.get("value") or "")
                value = str(option.get("value") or option.get("label") or label)
            else:
                label = str(getattr(option, "label", ""))
                value = str(getattr(option, "value", label))
            if value:
                options[value] = {"value": value, "label": label or value}
        qtype = self._question_type_value(question).strip().lower()
        if qtype.startswith("single-select"):
            if values:
                value = next(iter(values))
                option = options.get(value)
                if option:
                    session.answers[question.qid] = dict(option)
                else:
                    session.answers.pop(question.qid, None)
            else:
                session.answers.pop(question.qid, None)
            return

        selected: list[dict[str, str]] = []
        for token in values:
            option = options.get(token)
            if option:
                selected.append(dict(option))
        if selected:
            session.answers[question.qid] = selected
        else:
            session.answers.pop(question.qid, None)

    def _modal_title_prefix(self) -> str:
        return "Onboarding questions"

    def _modal_intro_text(self) -> str:
        return "🧭 Hold on we´ll be ready for you in a second."

    def _select_intro_text(self) -> str:
        return "🔽 Choose the options that apply using the menus below."

    def _resolve_summary_author(
        self, thread: discord.Thread, interaction: discord.Interaction
    ) -> discord.Member:
        owner = getattr(thread, "owner", None)
        if isinstance(owner, discord.Member):
            return owner

        guild = thread.guild
        owner_id = getattr(thread, "owner_id", None)
        if guild and owner_id:
            member = guild.get_member(owner_id)
            if member is not None:
                return member

        user = interaction.user
        if isinstance(user, discord.Member):
            return user

        if guild is not None and getattr(user, "id", None):
            member = guild.get_member(int(user.id))
            if member is not None:
                return member

        if guild is not None:
            me = guild.me
            if me is not None:
                return me

        user_obj = interaction.user
        if isinstance(user_obj, discord.User):
            return cast(discord.Member, user_obj)

        raise TypeError("Unable to resolve summary author for onboarding thread")


class WelcomeController(BaseWelcomeController):
    def __init__(self, bot: commands.Bot) -> None:
        super().__init__(bot, flow="welcome")


class _PreviewView(discord.ui.View):
    def __init__(self, controller: BaseWelcomeController, thread_id: int) -> None:
        super().__init__(timeout=600)
        self.controller = controller
        self.thread_id = thread_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # pragma: no cover - network
        allowed, _ = await self.controller.check_interaction(self.thread_id, interaction)
        return allowed

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, custom_id="ob.confirm")
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:  # pragma: no cover - network
        if getattr(interaction, "_c1c_claimed", False):
            return
        setattr(interaction, "_c1c_claimed", True)
        thread = interaction.channel if isinstance(interaction.channel, discord.Thread) else None
        context = {
            **logs.thread_context(thread if isinstance(thread, discord.Thread) else None),
            "actor": logs.format_actor(interaction.user),
            "view": "preview",
            "view_id": "ob.confirm",
        }
        actor_name = logs.format_actor_handle(interaction.user)
        if actor_name:
            context["actor_name"] = actor_name
        await logs.send_welcome_log("debug", result="clicked", **context)
        await self.controller._handle_confirm(self.thread_id, interaction)

    @discord.ui.button(label="Edit", style=discord.ButtonStyle.secondary, custom_id="ob.edit")
    async def edit(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:  # pragma: no cover - network
        if getattr(interaction, "_c1c_claimed", False):
            return
        setattr(interaction, "_c1c_claimed", True)
        thread = interaction.channel if isinstance(interaction.channel, discord.Thread) else None
        context = {
            **logs.thread_context(thread if isinstance(thread, discord.Thread) else None),
            "actor": logs.format_actor(interaction.user),
            "view": "preview",
            "view_id": "ob.edit",
        }
        actor_name = logs.format_actor_handle(interaction.user)
        if actor_name:
            context["actor_name"] = actor_name
        await logs.send_welcome_log("debug", result="clicked", **context)
        await self.controller._handle_edit(self.thread_id, interaction)

    async def on_timeout(self) -> None:  # pragma: no cover - network
        for child in self.children:
            child.disabled = True


async def _safe_ephemeral(interaction: discord.Interaction, message: str) -> None:
    diag_state = diag.interaction_state(interaction)
    response_done = False
    try:
        response_done = bool(interaction.response.is_done())
    except Exception:  # pragma: no cover - defensive guard
        response_done = False
    deny_path = "followup" if response_done else "initial_response"
    diag_state["deny_path"] = deny_path
    diag_state["response_is_done"] = response_done
    if diag.is_enabled():
        await diag.log_event("info", "deny_notice_pre", **diag_state)
    try:
        if response_done:
            await _edit_deferred_response(interaction, message)
        else:
            await interaction.response.send_message(message, ephemeral=True)
        if diag.is_enabled():
            await diag.log_event("info", "deny_notice_sent", **diag_state)
    except Exception:  # pragma: no cover - defensive network handling
        if diag.is_enabled():
            error_type = None
            status = None
            code = None
            if isinstance(exc := sys.exc_info()[1], discord.Forbidden):
                error_type = "Forbidden"
                status = getattr(exc, "status", None)
                code = getattr(exc, "code", None)
            elif isinstance(exc, discord.HTTPException):
                error_type = "HTTPException"
                status = getattr(exc, "status", None)
                code = getattr(exc, "code", None)
            else:
                error_type = exc.__class__.__name__ if exc else None
            await diag.log_event(
                "error",
                "deny_notice_error",
                error_type=error_type,
                status=status,
                code=code,
                **diag_state,
            )
        log.warning("failed to send denial response", exc_info=True)


def _visible_state(visibility: dict[str, dict[str, str]], qid: str) -> str:
    return visibility.get(qid, {}).get("state", "show")


def _is_effectively_required(
    question: Question, visibility: dict[str, dict[str, str]]
) -> bool:
    info = visibility.get(question.qid) or {}
    if "required" in info:
        return bool(info["required"])
    state = info.get("state")
    if state == "optional":
        return False
    return bool(getattr(question, "required", False))


def _preview_value_for_question(question: Question, stored: Any) -> str:
    if stored is None:
        return ""
    qtype = getattr(question, "type", None) or getattr(question, "qtype", None)
    qtype_text = str(qtype or "").strip().lower()
    if qtype_text in TEXT_TYPES:
        return str(stored)
    if question.type == "bool":
        if isinstance(stored, bool):
            return "Yes" if stored else "No"
        text = str(stored).strip()
        lowered = text.lower()
        if lowered in {"true", "yes", "y", "1"}:
            return "Yes"
        if lowered in {"false", "no", "n", "0"}:
            return "No"
        return text
    if question.type == "single-select":
        if isinstance(stored, dict):
            label = stored.get("label") or stored.get("value")
            return str(label or "")
        return str(stored)
    if isinstance(stored, str):
        return stored.strip()
    if isinstance(stored, Iterable):
        labels = []
        for item in stored:
            if isinstance(item, dict):
                label = item.get("label") or item.get("value")
                if label:
                    labels.append(str(label))
            else:
                labels.append(str(item))
        return ", ".join(labels)
    return str(stored)


def _missing_required_selects(
    questions: Sequence[Question],
    visibility: dict[str, dict[str, str]],
    answers: dict[str, Any],
) -> list[str]:
    missing: list[str] = []
    for question in questions:
        qtype = getattr(question, "type", None) or getattr(question, "qtype", None)
        if str(qtype or "").strip().lower() not in SELECT_TYPES:
            continue
        state = _visible_state(visibility, question.qid)
        if state == "skip":
            continue
        required = _is_effectively_required(question, visibility)
        if not required:
            continue
        value = answers.get(question.qid)
        if not _has_select_answer(value):
            missing.append(question.label)
    return missing


def _has_select_answer(value: Any) -> bool:
    if not value:
        return False
    if isinstance(value, dict):
        if value.get("value"):
            return True
        nested = value.get("values")
        if isinstance(nested, Iterable):
            return any(_has_select_answer(item) for item in nested)
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Iterable):
        return any(_has_select_answer(item) for item in value)
    return bool(value)


def _final_fields(questions: Sequence[Question], answers: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    option_lookup: dict[str, dict[str, str]] = {}
    for question in questions:
        qtype = getattr(question, "type", None) or getattr(question, "qtype", None)
        if str(qtype or "").strip().lower() in SELECT_TYPES:
            lookup: dict[str, str] = {}
            for option in getattr(question, "options", ()):  # type: ignore[attr-defined]
                label = getattr(option, "label", None)
                value = getattr(option, "value", None)
                if value:
                    lookup[str(value)] = str(label if label is not None else value)
            if not lookup:
                note = getattr(question, "note", None)
                if isinstance(note, str) and note.strip():
                    for token in note.replace("\n", ",").split(","):
                        text = token.strip()
                        if text:
                            lookup[text] = text
            option_lookup[question.qid] = lookup
    for qid, value in answers.items():
        label_value = _convert_to_labels(value, option_lookup.get(qid, {}))
        if isinstance(label_value, str) and len(label_value) > 300:
            label_value = label_value[:297] + "..."
        fields[qid] = label_value
    return fields


def _convert_to_labels(value: Any, mapping: dict[str, str]) -> Any:
    if isinstance(value, dict):
        if "value" in value:
            token = value.get("value")
            return mapping.get(token, str(value.get("label") or token))
        if "values" in value:
            items = value.get("values")
            if isinstance(items, Iterable):
                return [
                    mapping.get(item.get("value"), item.get("label"))
                    if isinstance(item, dict)
                    else mapping.get(str(item), str(item))
                    for item in items
                ]
        return {key: _convert_to_labels(val, mapping) for key, val in value.items()}
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
        return [_convert_to_labels(item, mapping) for item in value]
    if isinstance(value, str):
        return mapping.get(value, value)
    return value


def _extract_level_detail(answers: Mapping[str, Any]) -> str | None:
    raw = answers.get("w_level_detail") if isinstance(answers, Mapping) else None
    if isinstance(raw, Mapping):
        for key in ("label", "value", "text"):
            candidate = raw.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    if isinstance(raw, (list, tuple)):
        for item in raw:
            if isinstance(item, str) and item.strip():
                return item.strip()
            if isinstance(item, Mapping):
                for key in ("label", "value", "text"):
                    candidate = item.get(key)
                    if isinstance(candidate, str) and candidate.strip():
                        return candidate.strip()
    if raw is not None:
        text = str(raw).strip()
        if text:
            return text
    return None


def _safe_followup(interaction: discord.Interaction, message: str) -> Awaitable[None]:
    async def runner() -> None:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except Exception:
            log.warning("failed to send follow-up", exc_info=True)

    return runner()


__all__ = ["WelcomeController", "BaseWelcomeController"]
