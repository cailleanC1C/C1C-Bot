"""Persistent UI components for the onboarding welcome panel."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Iterable, Optional, Sequence

import discord
from discord.ext import commands

from modules.onboarding.ui.panel_message_manager import PanelMessageManager

# -- inline status text helper (Waiting / Saved / Invalid) --
def _status_for(
    is_answered: bool,
    is_valid: bool,
    validation_error,
    *,
    override: str | None = None,
) -> str:
    if override:
        return override
    if validation_error or not is_valid:
        if isinstance(validation_error, str) and validation_error.strip():
            hint = validation_error.strip()
        else:
            hint = (
                getattr(validation_error, "hint", None)
                or getattr(validation_error, "message", None)
                or (str(validation_error).strip() if validation_error else None)
                or "Check the format."
            )
        return f"⚠️ Invalid format: {hint}"
    if not is_answered:
        return "Waiting for your reply."
    return "✅ Saved. Click Next."

from c1c_coreops import rbac  # Retained for compatibility with existing tests/hooks.
from modules.onboarding import diag, logs

__all__ = [
    "OPEN_QUESTIONS_CUSTOM_ID",
    "WELCOME_PANEL_TAG",
    "OnboardWizard",
    "OpenQuestionsPanelView",
    "WelcomePanel",
    "bind_controller",
    "find_panel_message",
    "get_controller",
    "get_panel_message_id",
    "is_panel_live",
    "mark_panel_inactive_by_message",
    "register_panel_message",
    "register_persistent_views",
    "register_views",
    "unbind_controller",
]

log = logging.getLogger("c1c.onboarding.ui.panels")

OPEN_QUESTIONS_CUSTOM_ID = "welcome.panel.open"
WELCOME_PANEL_TAG = "welcome_panel"


class _ControllerProtocol:
    async def check_interaction(
        self, thread_id: int, interaction: discord.Interaction, *, context: dict[str, Any] | None = None
    ) -> tuple[bool, str | None]:  # pragma: no cover - protocol
        ...

    async def _handle_modal_launch(self, thread_id: int, interaction: discord.Interaction) -> None:  # pragma: no cover - protocol
        ...

    def render_step(self, thread_id: int | None, step: int) -> str:  # pragma: no cover - protocol
        ...

    async def capture_step(self, interaction: discord.Interaction, thread_id: int | None, step: int) -> None:  # pragma: no cover - protocol
        ...

    def is_finished(self, thread_id: int | None, step: int) -> bool:  # pragma: no cover - protocol
        ...

    async def finish_and_summarize(self, interaction: discord.Interaction, thread_id: int | None) -> None:  # pragma: no cover - protocol
        ...


_CONTROLLERS: Dict[int, _ControllerProtocol] = {}
_PANEL_MESSAGES: Dict[int, int] = {}
_ACTIVE_PANEL_MESSAGE_IDS: set[int] = set()
_REGISTRATION_COUNTS: Dict[str, int] = {}


class _DefaultControllerRegistry:
    def __init__(self, view: "OpenQuestionsPanelView") -> None:
        self._view = view

    async def get_or_create(self, thread_id: int | None) -> _ControllerProtocol:
        controller = self._view._controller
        if controller is not None:
            return controller
        if thread_id is None:
            raise LookupError("thread_missing")
        key = int(thread_id)
        controller = _CONTROLLERS.get(key)
        if controller is None:
            raise LookupError("controller_missing")
        return controller


def _display_name(user: discord.abc.User | discord.Member | None) -> str:
    if user is None:
        return "<unknown>"
    return getattr(user, "display_name", None) or getattr(user, "global_name", None) or getattr(user, "name", None) or "<unknown>"


def _channel_path(channel: discord.abc.GuildChannel | discord.Thread | None) -> str:
    if isinstance(channel, discord.Thread):
        parent = getattr(channel, "parent", None)
        parent_label = f"#{getattr(parent, 'name', 'unknown')}" if parent else "#unknown"
        return f"{parent_label} › {getattr(channel, 'name', 'thread')}"
    if isinstance(channel, discord.abc.GuildChannel):
        return f"#{getattr(channel, 'name', 'channel')}"
    return "#unknown"


def _visible_state(visibility: dict[str, dict[str, str]] | None, qid: str | None) -> str:
    if not visibility or not isinstance(visibility, dict) or not qid:
        return "show"
    entry = visibility.get(qid) or {}
    if isinstance(entry, dict):
        state = entry.get("state")
        if isinstance(state, str) and state:
            return state
        required = entry.get("required")
        if required is False:
            return "optional"
    return "show"


def _log_followup_fallback(
    interaction: discord.Interaction,
    *,
    action: str,
    error: Exception,
) -> None:
    user_name = _display_name(getattr(interaction, "user", None))
    channel = _channel_path(getattr(interaction, "channel", None))
    why = getattr(error, "__class__", type(error)).__name__
    message = f"⚠️ Welcome — followup fallback • action={action} • user={user_name} • channel={channel} • why={why}"
    log.warning(message)


async def _edit_original_response(
    interaction: discord.Interaction,
    *,
    content: str,
) -> None:
    try:
        await interaction.edit_original_response(content=content)
    except Exception as exc:  # pragma: no cover - defensive fallback
        _log_followup_fallback(interaction, action="edit_original", error=exc)
        followup = getattr(interaction, "followup", None)
        if followup is None:
            log.debug("followup handler missing; skipping deferred notice")
            return
        try:
            await followup.send(content, ephemeral=True)
        except Exception:  # pragma: no cover - final guard
            log.warning("failed to send followup notice", exc_info=True)


async def _defer_interaction(interaction: discord.Interaction) -> None:
    try:
        await interaction.response.defer(ephemeral=True)
    except discord.Forbidden:
        await interaction.response.defer()
    except discord.InteractionResponded:
        return


async def _ensure_deferred(interaction: discord.Interaction) -> None:
    """Ack the interaction if it isn't already. Never raises."""
    try:
        if not interaction.response.is_done():
            # thinking=True keeps the interaction alive without posting text
            await interaction.response.defer(thinking=True)
    except Exception:
        # If it was already acknowledged or Discord races, just continue
        pass


def _restart_context_from_state(
    interaction: discord.Interaction,
    state: dict[str, Any],
) -> dict[str, Any]:
    context = {"view": "panel", "view_tag": WELCOME_PANEL_TAG, "custom_id": "welcome.panel.restart"}
    channel = getattr(interaction, "channel", None)
    thread = channel if isinstance(channel, discord.Thread) else None
    context.update(logs.thread_context(thread))
    message = getattr(interaction, "message", None)
    if message is not None:
        message_id = getattr(message, "id", None)
        if message_id is not None:
            try:
                context["message_id"] = int(message_id)
            except (TypeError, ValueError):
                context["message_id"] = message_id
    actor_name = logs.format_actor_handle(getattr(interaction, "user", None))
    if actor_name:
        context["actor_name"] = actor_name
    context["state_view"] = state.get("view")
    return context


def _claim_interaction(interaction: discord.Interaction) -> bool:
    """Mark this interaction as claimed by the welcome panel handler."""

    # NOTE: discord.Interaction does not expose ``_c1c_claimed`` by default.
    # Always guard with ``getattr`` before first use.
    if getattr(interaction, "_c1c_claimed", False):
        return False
    try:
        setattr(interaction, "_c1c_claimed", True)
    except Exception:
        # As a last resort, allow the flow to continue even if we cannot mark
        # the ad-hoc attribute.
        return True
    return True


def bind_controller(thread_id: int, controller: _ControllerProtocol) -> None:
    _CONTROLLERS[thread_id] = controller


def unbind_controller(thread_id: int) -> None:
    _CONTROLLERS.pop(thread_id, None)
    message_id = _PANEL_MESSAGES.pop(thread_id, None)
    if message_id is not None:
        _ACTIVE_PANEL_MESSAGE_IDS.discard(message_id)


def get_controller(thread_id: int | None) -> _ControllerProtocol | None:
    if thread_id is None:
        return None
    return _CONTROLLERS.get(thread_id)


def register_panel_message(thread_id: int, message_id: int) -> None:
    _PANEL_MESSAGES[thread_id] = message_id
    _ACTIVE_PANEL_MESSAGE_IDS.add(message_id)


def get_panel_message_id(thread_id: int) -> Optional[int]:
    return _PANEL_MESSAGES.get(thread_id)


async def find_panel_message(
    thread: discord.Thread, *, bot_user_id: int | None
) -> Optional[discord.Message]:
    """Return an existing panel message authored by the bot, if any.

    The search is intentionally shallow (up to the last 20 messages) to keep
    network calls predictable. The helper mirrors the logic used by the fallback
    cog and ensures panel creation is idempotent across welcome and promo
    triggers.
    """

    history = getattr(thread, "history", None)
    if bot_user_id is None or history is None or not callable(history):
        return None

    async for message in history(limit=20):
        author = getattr(message, "author", None)
        if author is None or getattr(author, "id", None) != bot_user_id:
            continue
        for row in getattr(message, "components", []) or []:
            for component in getattr(row, "children", []) or []:
                if getattr(component, "custom_id", None) == OPEN_QUESTIONS_CUSTOM_ID:
                    return message
        for component in getattr(message, "components", []) or []:
            if getattr(component, "custom_id", None) == OPEN_QUESTIONS_CUSTOM_ID:
                return message
    return None


def is_panel_live(message_id: int | None) -> bool:
    if message_id is None:
        return False
    return message_id in _ACTIVE_PANEL_MESSAGE_IDS


def mark_panel_inactive_by_message(message_id: int | None) -> None:
    if message_id is None:
        return
    _ACTIVE_PANEL_MESSAGE_IDS.discard(message_id)
    to_delete = [thread_id for thread_id, mid in _PANEL_MESSAGES.items() if mid == message_id]
    for thread_id in to_delete:
        _PANEL_MESSAGES.pop(thread_id, None)


def _component_counts(view: discord.ui.View) -> tuple[int, int, int]:
    buttons = sum(1 for child in view.children if isinstance(child, discord.ui.Button))
    text_inputs = sum(1 for child in view.children if isinstance(child, discord.ui.TextInput))
    selects = sum(1 for child in view.children if isinstance(child, discord.ui.Select))
    return buttons, text_inputs, selects


def register_persistent_views(bot: discord.Client) -> dict[str, Any]:
    view = OpenQuestionsPanelView()
    started = time.perf_counter()
    registered = False
    duplicate = False
    stacksite: str | None = None
    error: Exception | None = None
    custom_ids: list[str] = []
    try:
        bot.add_view(view)
        registered = True
    except Exception as exc:  # pragma: no cover - defensive logging
        log.warning("failed to register persistent welcome panel view", exc_info=True)
        error = exc
    finally:
        if diag.is_enabled():
            key = view.__class__.__name__
            _REGISTRATION_COUNTS[key] = _REGISTRATION_COUNTS.get(key, 0) + 1
            duplicate = _REGISTRATION_COUNTS[key] > 1
            if duplicate:
                stacksite = diag.relative_stack_site(frame_level=2)
            custom_ids = [
                child.custom_id
                for child in view.children
                if isinstance(child, discord.ui.Button) and child.custom_id
            ]
            fields = {
                "view": key,
                "registered": registered,
                "timeout": view.timeout,
                "disable_on_timeout": getattr(view, "disable_on_timeout", None),
                "custom_ids": custom_ids,
            }
            if duplicate:
                fields["duplicate_registration"] = True
                if stacksite:
                    fields["stacksite"] = stacksite
            diag.log_event_sync("info", "persistent_view_registered", **fields)

    duration_ms = int((time.perf_counter() - started) * 1000)
    buttons, text_inputs, selects = _component_counts(view)
    components = f"buttons:{buttons},textinputs:{text_inputs},selects:{selects}"

    info: dict[str, Any] = {
        "view": view.__class__.__name__,
        "components": components,
        "duration_ms": duration_ms,
        "registered": registered,
        "duplicate_registration": duplicate,
        "error": error,
        "timeout": view.timeout,
        "disable_on_timeout": getattr(view, "disable_on_timeout", None),
        "custom_ids": custom_ids,
    }
    if stacksite:
        info["stacksite"] = stacksite
    return info


class OpenQuestionsPanelView(discord.ui.View):
    """Persistent panel view that launches the welcome modal flow."""

    CUSTOM_ID = OPEN_QUESTIONS_CUSTOM_ID
    ERROR_NOTICE = (
        "Couldn\u2019t open the questions just now. A recruiter has been pinged. Please try again in a moment."
    )
    _wizard_messages: dict[int, int] = {}

    def __init__(
        self,
        *,
        controller: _ControllerProtocol | None = None,
        thread_id: int | None = None,
        controller_registry: Any | None = None,
        target_user_id: int | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self._controller = controller
        self._thread_id = thread_id
        self._controller_registry = controller_registry or _DefaultControllerRegistry(self)
        self.disable_on_timeout = False
        self._error_notice_ids: set[int] = set()
        self._target_user_id: int | None = None
        self._resume_button: discord.ui.Button | None = None
        session_state = getattr(controller, "session", {}) if controller else {}
        self._manager = PanelMessageManager(session_state)
        if diag.is_enabled():
            diag.log_event_sync(
                "debug",
                "panel_view_initialized",
                view=self.__class__.__name__,
                timeout=self.timeout,
                disable_on_timeout=self.disable_on_timeout,
                thread_id=int(thread_id) if thread_id is not None else None,
            )
        self._update_resume_visibility(thread_id, target_user_id)

    def _attach_controller(
        self, controller: _ControllerProtocol, *, thread_id: int | None = None
    ) -> None:
        self._controller = controller
        if thread_id is not None:
            try:
                self._thread_id = int(thread_id)
            except (TypeError, ValueError):
                self._thread_id = thread_id
        self._update_resume_visibility(self._thread_id, self._target_user_id)
        session_state = getattr(controller, "session", {}) if controller else {}
        self._manager = PanelMessageManager(session_state)

    @staticmethod
    def _session_exists(thread_id: int, user_id: int) -> bool:
        try:
            from modules.onboarding.sessions import Session

            session = Session.load_from_sheet(user_id, thread_id)
        except Exception:
            return False
        if not session:
            return False
        return not getattr(session, "completed", False)

    def _create_resume_button(self) -> discord.ui.Button:
        button = discord.ui.Button(
            label="Resume",
            style=discord.ButtonStyle.secondary,
            custom_id="welcome.panel.resume",
        )

        async def _resume_callback(interaction: discord.Interaction) -> None:
            await self._resume_from_button(interaction)

        button.callback = _resume_callback  # type: ignore[assignment]
        return button

    def _remove_resume_button(self) -> None:
        if self._resume_button is None:
            return
        try:
            self.remove_item(self._resume_button)
        except ValueError:
            pass
        self._resume_button = None

    def _update_resume_visibility(
        self, thread_id: int | None, user_id: int | None
    ) -> tuple[bool, bool]:
        base_thread = thread_id if thread_id is not None else self._thread_id
        base_user = user_id if user_id is not None else self._target_user_id

        try:
            resolved_thread = int(base_thread) if base_thread is not None else None
        except (TypeError, ValueError):
            resolved_thread = None
        try:
            resolved_user = int(base_user) if base_user is not None else None
        except (TypeError, ValueError):
            resolved_user = None

        if resolved_thread is not None:
            self._thread_id = resolved_thread
        if resolved_user is not None:
            self._target_user_id = resolved_user

        has_session = False
        changed = False
        if resolved_thread is None or resolved_user is None:
            if self._resume_button is not None:
                self._remove_resume_button()
                changed = True
            return has_session, changed

        has_session = self._session_exists(resolved_thread, resolved_user)
        if has_session:
            if self._resume_button is None:
                self._resume_button = self._create_resume_button()
                self.add_item(self._resume_button)
                changed = True
        elif self._resume_button is not None:
            self._remove_resume_button()
            changed = True

        return has_session, changed

    async def _bootstrap_controller(
        self,
        interaction: discord.Interaction,
        thread_id: int | None,
        channel: discord.abc.GuildChannel | discord.Thread | None,
    ) -> _ControllerProtocol | None:
        if thread_id is None or not isinstance(channel, discord.Thread):
            return None

        try:
            from modules.onboarding.welcome_flow import start_welcome_dialog
        except Exception:
            log.warning("welcome bootstrap import failed", exc_info=True)
            return None

        message = getattr(interaction, "message", None)
        panel_message = message if isinstance(message, discord.Message) else None
        panel_id: int | None
        raw_id = getattr(panel_message, "id", None)
        try:
            panel_id = int(raw_id) if raw_id is not None else None
        except (TypeError, ValueError):
            panel_id = None

        try:
            await start_welcome_dialog(
                channel,
                getattr(interaction, "user", None),
                "panel_button",
                bot=getattr(interaction, "client", None),
                panel_message_id=panel_id,
                panel_message=panel_message,
            )
        except Exception as exc:
            if diag.is_enabled():
                await diag.log_event(
                    "warning",
                    "panel_bootstrap_failed",
                    thread_id=thread_id,
                    error=str(exc),
                )
            log.warning("failed to bootstrap welcome controller", exc_info=True)
            return None

        try:
            controller = await self._controller_registry.get_or_create(thread_id)
        except Exception:
            return None

        if diag.is_enabled():
            await diag.log_event(
                "info",
                "panel_bootstrap_success",
                thread_id=thread_id,
            )

        return controller

    async def _resume_from_button(self, interaction: discord.Interaction) -> None:
        channel = getattr(interaction, "channel", None)
        thread_identifier = getattr(channel, "id", None) if isinstance(channel, discord.Thread) else None
        user_identifier = getattr(getattr(interaction, "user", None), "id", None)

        has_session, changed = self._update_resume_visibility(thread_identifier, user_identifier)
        if changed:
            message = getattr(interaction, "message", None)
            if message is not None:
                try:
                    await message.edit(view=self)
                except Exception:
                    pass

        await self._launch_logic(interaction)

        if not has_session:
            followup = getattr(interaction, "followup", None)
            if followup is not None:
                try:
                    await followup.send(
                        "No saved onboarding session found — starting a new one.",
                        ephemeral=True,
                    )
                except Exception:
                    pass

    def _resolve(self, interaction: discord.Interaction) -> tuple[_ControllerProtocol | None, int | None]:
        thread_id = self._thread_id or getattr(interaction.channel, "id", None) or interaction.channel_id
        controller = self._controller or _CONTROLLERS.get(int(thread_id) if thread_id is not None else None)
        if controller is None and thread_id is not None:
            controller = _CONTROLLERS.get(int(thread_id))
        return controller, int(thread_id) if thread_id is not None else None

    def as_disabled(self, *, label: str | None = None) -> "OpenQuestionsPanelView":
        """Return a disabled clone of this view for optimistic locking."""

        clone = self.__class__(
            controller=self._controller,
            thread_id=self._thread_id,
            target_user_id=self._target_user_id,
        )
        for child in clone.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True
                if label and getattr(child, "custom_id", None) == OPEN_QUESTIONS_CUSTOM_ID:
                    child.label = label
        return clone

    @classmethod
    async def refresh_enabled(
        cls,
        interaction: discord.Interaction | None,
        *,
        controller: _ControllerProtocol | None,
        thread_id: int | None,
    ) -> None:
        if interaction is None:
            return
        view = cls(controller=controller, thread_id=thread_id, target_user_id=getattr(controller, "target_user_id", None))
        response = getattr(interaction, "response", None)
        if response is not None:
            is_done = getattr(response, "is_done", None)
            responded = False
            if callable(is_done):
                try:
                    responded = bool(is_done())
                except Exception:
                    responded = False
            elif isinstance(is_done, bool):
                responded = is_done
            if not responded:
                try:
                    await response.edit_message(view=view)
                    return
                except Exception:
                    log.debug("failed to refresh panel via interaction response", exc_info=True)
        message = getattr(interaction, "message", None)
        if isinstance(message, discord.Message):
            try:
                await message.edit(view=view)
            except Exception:
                log.warning("failed to refresh panel message", exc_info=True)

    async def _restore_enabled(self, interaction: discord.Interaction) -> None:
        await self.__class__.refresh_enabled(
            interaction,
            controller=self._controller,
            thread_id=self._thread_id,
        )

    def _recompute_nav_state(self) -> None:
        controller = self._controller
        state = "attached" if controller is not None else "missing"
        try:
            logs.human("info", "onboarding.ui_nav_state", controller=state)
        except Exception:
            pass

    @discord.ui.button(
        label="Open questions",
        style=discord.ButtonStyle.primary,
        custom_id=OPEN_QUESTIONS_CUSTOM_ID,
    )
    async def launch(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        """Start the inline onboarding wizard inside the active thread."""

        await self._launch_logic(interaction)

    async def _launch_logic(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            try:
                await interaction.response.defer()
            except Exception:
                pass

        channel = getattr(interaction, "channel", None)
        thread_identifier = getattr(channel, "id", None) if isinstance(channel, discord.Thread) else None
        if thread_identifier is None:
            thread_identifier = self._thread_id
        try:
            thread_key = int(thread_identifier) if thread_identifier is not None else None
        except (TypeError, ValueError):
            thread_key = None

        actor_id = getattr(getattr(interaction, "user", None), "id", None)
        self._update_resume_visibility(thread_key, actor_id)

        try:
            controller = await self._controller_registry.get_or_create(thread_key)
        except LookupError as exc:
            controller = await self._bootstrap_controller(interaction, thread_key, channel)
            if controller is None:
                await self._ensure_error_notice_followup(interaction, reason="controller_missing")
                logs.human(
                    "error",
                    "onboarding.launch_controller_missing",
                    thread_id=thread_key,
                    error=str(exc),
                )
                await self._ensure_error_notice(interaction)
                return
        except Exception as exc:
            await self._ensure_error_notice_followup(interaction, reason="controller_missing")
            logs.human(
                "error",
                "onboarding.launch_controller_missing",
                thread_id=thread_key,
                error=str(exc),
            )
            await self._ensure_error_notice(interaction)
            return

        wait_ready = getattr(controller, "wait_until_ready", None)
        ready = True
        if callable(wait_ready):
            try:
                ready = await wait_ready(timeout=2.0)
            except Exception:
                ready = False
        if not ready:
            logs.human("warn", "onboarding.launch_wait_timeout", thread_id=thread_key)

        try:
            self._attach_controller(controller, thread_id=thread_key)
            self._recompute_nav_state()
            message = getattr(interaction, "message", None)
            if message is not None:
                await message.edit(view=self)
        except Exception as err:
            await self._ensure_error_notice_followup(interaction, reason="edit_failed")
            logs.human("error", "onboarding.launch_edit_failed", error=str(err))
            return

        controller, thread_id = self._resolve(interaction)
        if controller is None or thread_id is None:
            await self._restart_from_view(interaction, log_context="launch_resolve_failed")
            return

        diag_state = diag.interaction_state(interaction)
        diag_state["thread_id"] = thread_id
        diag_state["custom_id"] = OPEN_QUESTIONS_CUSTOM_ID

        channel = getattr(interaction, "channel", None)
        if not isinstance(channel, discord.Thread):
            await self._ensure_error_notice(interaction)
            return

        async def _hard_fail(reason: str, *, error: Exception | None = None) -> None:
            if diag.is_enabled():
                payload = {"reason": reason, **diag_state}
                if error is not None:
                    payload["error"] = str(error)
                await diag.log_event("warning", "wizard_launch_failed", **payload)
            await self._ensure_error_notice(interaction)

        await _ensure_deferred(interaction)

        def _schema_failure_payload(error_text: str) -> dict[str, object]:
            payload = logs.thread_context(channel)
            payload.update(
                {
                    "actor": logs.format_actor(getattr(interaction, "user", None)),
                    "view": "panel",
                    "source": "panel",
                    "result": "schema_load_failed",
                    "error": error_text,
                }
            )
            actor_name = logs.format_actor_handle(getattr(interaction, "user", None))
            if actor_name:
                payload["actor_name"] = actor_name
            return payload

        loader = getattr(controller, "get_or_load_questions", None)
        thread_questions: Sequence[Any] | None = None
        try:
            if callable(loader):
                thread_questions = await loader(thread_id)
        except KeyError as exc:
            log.warning("⚠️ onboarding wizard config error: %s", exc)
            message = str(exc.args[0]) if exc.args else str(exc)
            payload = _schema_failure_payload(message)
            await logs.send_welcome_exception("error", exc, **payload)
            await _hard_fail("config_missing", error=exc)
            return
        except RuntimeError as exc:
            message = str(exc).strip()
            if "cache is empty" in message:
                payload = _schema_failure_payload(message)
                await logs.send_welcome_exception("error", exc, **payload)
                await _hard_fail("cache_empty", error=exc)
                return
            log.warning("onboarding question preload failed", exc_info=True)
            await _hard_fail("preload_exception", error=exc)
            return
        except Exception as exc:  # defensive guard
            log.warning("onboarding question preload failed", exc_info=True)
            await _hard_fail("preload_exception", error=exc)
            return

        questions_dict = getattr(controller, "questions_by_thread", {})
        if not thread_questions and isinstance(questions_dict, dict):
            thread_questions = questions_dict.get(thread_id)
        if not thread_questions:
            log.warning(
                "⚠️ onboarding wizard: no questions available for thread %s", thread_id
            )
            error_message = "no onboarding questions mapped for thread"
            payload = _schema_failure_payload(error_message)
            await logs.send_welcome_exception(
                "error",
                RuntimeError(error_message),
                **payload,
            )
            await _hard_fail("no_questions")
            return

        view = OnboardWizard(controller=controller, thread_id=thread_id, step=0)

        async def _render_or_update_wizard(
            *, content: str, view: OnboardWizard
        ) -> discord.Message | None:
            existing_map = getattr(controller, "_inline_messages", None)
            id_map = getattr(controller, "_inline_message_ids", None)

            existing_message_id = self._wizard_messages.get(thread_id)
            existing: discord.Message | None = None

            fetcher = getattr(channel, "fetch_message", None)
            if existing_message_id and callable(fetcher):
                try:
                    existing = await fetcher(existing_message_id)
                except Exception:
                    existing = None

            if existing is None and isinstance(existing_map, dict):
                existing = existing_map.get(thread_id)

            if existing is None and isinstance(id_map, dict):
                message_id = id_map.get(thread_id)
                if message_id and callable(fetcher):
                    try:
                        existing = await fetcher(message_id)
                    except Exception:
                        id_map.pop(thread_id, None)

            wizard_message: discord.Message | None = None

            if existing is not None:
                try:
                    await existing.edit(content=content, view=view)
                    wizard_message = existing
                except Exception:
                    try:
                        wizard_message = await channel.send(content, view=view)
                    except Exception:
                        await _hard_fail("send_failed")
                        raise
            else:
                try:
                    wizard_message = await channel.send(content, view=view)
                except Exception:
                    await _hard_fail("send_failed")
                    raise

            if isinstance(existing_map, dict) and wizard_message is not None:
                existing_map[thread_id] = wizard_message
            if isinstance(id_map, dict) and wizard_message is not None:
                try:
                    id_map[thread_id] = int(getattr(wizard_message, "id", 0))
                except Exception:
                    id_map.pop(thread_id, None)
            if wizard_message is not None:
                try:
                    self._wizard_messages[thread_id] = int(wizard_message.id)
                except Exception:
                    pass
            return wizard_message

        try:
            content = controller.render_step(thread_id, step=0)
            content = view._apply_requirement_suffix(content, view._question())
        except Exception as err:  # pragma: no cover - best-effort fallback
            # Surface enough context so we can see *what* actually blew up.
            try:
                current_step = getattr(controller, "current_step", None)
            except Exception:  # pragma: no cover - defensive
                current_step = None

            log.warning(
                "failed to render onboarding wizard step • flow=%r step=%r error=%r",
                getattr(controller, "flow_id", None),
                current_step,
                err,
                exc_info=True,
            )
            logs.human(
                "error",
                "onboarding.wizard_render_failed",
                flow=getattr(controller, "flow_id", None),
                step=current_step,
                error=str(err),
            )
            await _hard_fail("render_failed", error=err)
            return

        message: discord.Message | None = None
        message = await _render_or_update_wizard(content=content, view=view)

        if isinstance(message, discord.Message):
            view.attach(message)

        if diag.is_enabled():
            await diag.log_event("info", "wizard_launch_sent", **diag_state)

    async def _post_retry_start(self, interaction: discord.Interaction, *, reason: str) -> None:
        """Post a small prompt with a retry button that uses a fresh interaction."""

        controller, thread_id = self._resolve(interaction)
        channel = getattr(interaction, "channel", None)
        if channel is None or not hasattr(channel, "send"):
            return

        if controller is None or thread_id is None:
            return

        if diag.is_enabled():
            await diag.log_event(
                "info",
                "retry_prompt_posted",
                thread_id=thread_id,
                reason=reason,
            )

        existing_retry_id = None
        retry_registry = getattr(controller, "retry_message_ids", None)
        if isinstance(retry_registry, dict):
            existing_retry_id = retry_registry.get(thread_id)

        if existing_retry_id:
            try:
                message = await channel.fetch_message(existing_retry_id)
                await message.delete()
            except Exception:  # pragma: no cover - best-effort cleanup
                pass

        from .views import RetryStartView  # local import to avoid circular dependency

        view = RetryStartView(controller, thread_id)
        try:
            message = await channel.send(
                "Let’s capture some details. Tap **Open questions** to begin.",
                view=view,
            )
        except Exception:  # pragma: no cover - network fallback
            log.warning("failed to post retry start prompt", exc_info=True)
            return

        if isinstance(retry_registry, dict):
            retry_registry[thread_id] = getattr(message, "id", None)

    @discord.ui.button(
        label="Restart",
        style=discord.ButtonStyle.secondary,
        custom_id="welcome.panel.restart",
    )
    async def restart(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await _defer_interaction(interaction)
        try:
            await self._handle_restart(interaction)
        except Exception:
            await self._ensure_error_notice(interaction)
            raise

    async def _handle_launch(
        self, interaction: discord.Interaction, *, response_was_done: bool = False
    ) -> None:
        controller, thread_id = self._resolve(interaction)
        if controller is None or thread_id is None:
            await self._restart_from_view(
                interaction, log_context={"reason": "launch_resolve_failed"}
            )
            return

        diag_state = diag.interaction_state(interaction)
        diag_state["thread_id"] = thread_id
        diag_state["custom_id"] = OPEN_QUESTIONS_CUSTOM_ID

        if response_was_done:
            if diag.is_enabled():
                await diag.log_event(
                    "warning",
                    "modal_launch_skipped",
                    skip_reason="interaction_already_responded",
                    **diag_state,
                )
            await self._post_retry_start(interaction, reason="response_done")
            return

        # Safe path: we haven’t answered yet → disable the button then continue
        try:
            await interaction.response.edit_message(view=self.as_disabled(label="Launching…"))
        except discord.InteractionResponded:
            # If already answered by something else, still proceed
            pass

        preload_questions = getattr(controller, "get_or_load_questions", None)
        questions_cache: Any = None
        cache_dict = getattr(controller, "questions_by_thread", None)
        if isinstance(cache_dict, dict):
            questions_cache = cache_dict.get(thread_id)
        if callable(preload_questions) and not questions_cache:
            try:
                await preload_questions(thread_id)
            except Exception as exc:  # pragma: no cover - best-effort preload
                await self._restore_enabled(interaction)
                if diag.is_enabled():
                    await diag.log_event(
                        "warning",
                        "onboard_preload_failed",
                        thread_id=thread_id,
                        error=str(exc),
                    )
                log.warning("welcome question preload failed", exc_info=True)
                await self._ensure_error_notice(interaction)
                return

        questions_dict = getattr(controller, "questions_by_thread", {})
        thread_questions = questions_dict.get(thread_id) if isinstance(questions_dict, dict) else None
        if not thread_questions:
            await self._restore_enabled(interaction)
            await self._ensure_error_notice(interaction)
            return

        # Rolling-card prototype replaces the inline wizard.
        starter = getattr(controller, "start_session_from_button", None)
        if callable(starter):
            try:
                await starter(
                    thread_id,
                    actor_id=getattr(getattr(interaction, "user", None), "id", None),
                    channel=getattr(interaction, "channel", None),
                    guild=getattr(interaction, "guild", None),
                    interaction=interaction,
                )
            except Exception:
                await self._restore_enabled(interaction)
                await self._ensure_error_notice(interaction)
                raise
            return

    async def _handle_restart(self, interaction: discord.Interaction) -> None:
        state = diag.interaction_state(interaction)
        controller = self._controller
        thread_id = state.get("thread_id")
        if not _claim_interaction(interaction):
            return
        if controller is None or thread_id is None:
            await self._restart_from_view(interaction, _restart_context_from_state(interaction, state))
            return

        controller_context = {"view": "panel", "view_tag": WELCOME_PANEL_TAG, "custom_id": "welcome.panel.restart"}
        try:
            allowed, _ = await controller.check_interaction(
                thread_id,
                interaction,
                context=controller_context,
            )
            if not allowed:
                return
        except Exception:
            await self._ensure_error_notice(interaction)
            raise

        await self._restart_from_view(interaction, _restart_context_from_state(interaction, state))

    async def on_timeout(self) -> None:  # pragma: no cover - network
        if diag.is_enabled():
            disabled_components = all(getattr(child, "disabled", False) for child in self.children)
            await diag.log_event(
                "warning",
                "panel_view_timeout",
                view=self.__class__.__name__,
                timeout=self.timeout,
                disable_on_timeout=getattr(self, "disable_on_timeout", None),
                thread_id=int(self._thread_id) if self._thread_id is not None else None,
                on_timeout_called=True,
                disabled_components=disabled_components,
                edit_attempted=False,
                post_attempted=False,
            )
        await super().on_timeout()

    async def _restart_from_view(
        self,
        interaction: discord.Interaction,
        log_context: dict[str, Any],
    ) -> None:
        thread = interaction.channel if isinstance(interaction.channel, discord.Thread) else None
        message = getattr(interaction, "message", None)
        message_id = getattr(message, "id", None)

        await self._notify_restart(interaction)

        controller = getattr(self, "controller", None)
        flow = getattr(controller, "flow", "welcome") if controller is not None else "welcome"

        async def _log_restart(result: str | None = None, reason: str | None = None) -> None:
            if flow != "welcome":
                return
            parent_channel = getattr(thread, "parent", None)
            question_count, schema_version = logs.question_stats("welcome")
            await logs.log_onboarding_panel_lifecycle(
                event="restart",
                ticket=thread,
                actor=getattr(interaction, "user", None),
                channel=parent_channel,
                questions=question_count,
                schema_version=schema_version,
                result=result,
                reason=reason,
            )

        restart_context = dict(log_context)
        restart_context["result"] = "restarted"
        if flow != "welcome":
            await logs.send_welcome_log("info", **restart_context)

        if not isinstance(thread, discord.Thread):
            if flow == "welcome":
                await _log_restart(result="error", reason="thread_missing")
            else:
                failure_context = dict(restart_context)
                failure_context["result"] = "error"
                failure_context["reason"] = "thread_missing"
                await logs.send_welcome_log("error", **failure_context)
            return

        if flow == "welcome":
            await _log_restart()

        try:
            from modules.onboarding.welcome_flow import start_welcome_dialog

            panel_id = None
            if message_id is not None:
                try:
                    panel_id = int(message_id)
                except (TypeError, ValueError):
                    panel_id = None
            panel_message = message if isinstance(message, discord.Message) else None
            await start_welcome_dialog(
                thread,
                interaction.user,
                "panel_restart",
                bot=interaction.client,
                panel_message_id=panel_id,
                panel_message=panel_message,
            )
        except Exception as exc:  # pragma: no cover - defensive logging path
            if flow == "welcome":
                log.exception("failed to restart welcome panel")
                await _log_restart(result="error", reason="restart_failed")
            else:
                error_context = dict(restart_context)
                error_context["result"] = "error"
                await logs.send_welcome_exception("error", exc, **error_context)

    async def _ensure_error_notice(self, interaction: discord.Interaction) -> None:
        identifier = getattr(interaction, "id", None)
        if identifier is None:
            identifier = id(interaction)
        try:
            key = int(identifier)
        except (TypeError, ValueError):
            key = id(interaction)
        if key in self._error_notice_ids:
            return
        self._error_notice_ids.add(key)
        if interaction.response.is_done():
            await _edit_original_response(interaction, content=self.ERROR_NOTICE)
            return
        try:
            await interaction.response.send_message(self.ERROR_NOTICE, ephemeral=True)
        except Exception:  # pragma: no cover - defensive logging
            log.warning("failed to send error notice", exc_info=True)

    async def _notify_error(self, interaction: discord.Interaction) -> None:
        await self._ensure_error_notice(interaction)

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        try:
            snapshot, perms_text, _ = diag.permission_snapshot(interaction)
        except Exception:
            snapshot = None
            perms_text = None

        channel = getattr(interaction, "channel", None)
        thread = channel if isinstance(channel, discord.Thread) else None
        parent_channel = getattr(thread, "parent", None) if thread is not None else None

        extra: dict[str, Any] = {
            "custom_id": getattr(item, "custom_id", None),
            "component_type": item.__class__.__name__ if item is not None else None,
            "message_id": getattr(interaction.message, "id", None)
            if interaction.message
            else None,
            "interaction_id": getattr(interaction, "id", None),
            "actor": logs.format_actor(interaction.user),
            "actor_id": getattr(interaction.user, "id", None),
            "actor_name": logs.format_actor_handle(interaction.user),
            "app_permissions": perms_text,
            "app_perms_text": perms_text,
            "app_permissions_snapshot": snapshot,
        }

        guild = getattr(interaction, "guild", None)
        guild_id = getattr(guild, "id", None) or getattr(interaction, "guild_id", None)
        if guild_id is not None:
            try:
                extra["guild_id"] = int(guild_id)
            except (TypeError, ValueError):
                extra["guild_id"] = guild_id

        if thread is not None:
            extra.update(logs.thread_context(thread))
            thread_id = getattr(thread, "id", None)
            if thread_id is not None:
                try:
                    extra["thread_id"] = int(thread_id)
                except (TypeError, ValueError):
                    extra["thread_id"] = thread_id
            parent_id = getattr(parent_channel, "id", None)
            if parent_id is not None:
                try:
                    extra["parent_channel_id"] = int(parent_id)
                except (TypeError, ValueError):
                    extra["parent_channel_id"] = parent_id
        elif channel is not None:
            channel_id = getattr(channel, "id", None)
            if channel_id is not None:
                try:
                    extra["channel_id"] = int(channel_id)
                except (TypeError, ValueError):
                    extra["channel_id"] = channel_id
            formatted = None
            try:
                formatted = logs.format_channel(channel)  # type: ignore[arg-type]
            except Exception:
                formatted = None
            if formatted:
                extra.setdefault("channel", formatted)

        await self._ensure_error_notice(interaction)
        logs.log_view_error(
            interaction,
            self,
            error,
            tag=WELCOME_PANEL_TAG,
            extra=extra,
        )

    async def _notify_restart(self, interaction: discord.Interaction) -> None:
        """Acknowledge the restart interaction without a confusing toast."""

        if interaction.response.is_done():
            return
        try:
            await _defer_interaction(interaction)
        except Exception:  # pragma: no cover - defensive logging
            log.warning("failed to defer restart notice", exc_info=True)


    async def _ensure_error_notice_followup(self, interaction: discord.Interaction, *, reason: str) -> None:
        try:
            followup = getattr(interaction, "followup", None)
            if followup is None:
                return
            await followup.send(
                "Couldn't open the questions just now. Please try again.",
                ephemeral=True,
            )
        except Exception:
            log.debug("failed to post error followup", exc_info=True)


    class OnboardWizard(discord.ui.View):
        _IDLE_REFRESH_SECONDS = 2 * 60 * 60
        _NOTICE_GLITCH = (
            "⚠️ Something glitched. I’ve refreshed the session. Press the button again. Your answers are safe."
        )
        _NOTICE_IDLE = "⏳ Session refreshed after being idle. Continue where you left off."
        _NOTICE_REFRESH = "🔄 Session refreshed. Try again."
        _NOTICE_MOVED = (
            "📌 Your session was moved to this message because the previous one disappeared. Continue."
        )

        def __init__(self, controller: _ControllerProtocol, thread_id: int, *, step: int = 0) -> None:
            super().__init__(timeout=None)
            self.controller = controller
            self.thread_id = thread_id
            self.step = step
            self._last_direction = 1
            self._message: discord.Message | None = None
            self._status_override: str | None = None
            self._status_error_hint: str | None = None
            self._status_question_key: str | None = None
            self._idle_task: asyncio.Task[None] | None = None
            self._last_touch: float = time.monotonic()
            # Build the controls immediately so attach()/refresh() behave like the legacy view.
            self._configure_components()
            self._schedule_idle_refresh()

        # --- Legacy surface (compat) -----------------------------------------
        def attach(self, message: discord.Message) -> None:
            """Bind the Discord message hosting this view for later refreshes."""

            self._message = message

        async def refresh(
            self,
            interaction: discord.Interaction | None = None,
            *,
            notice: str | None = None,
            touch: bool = True,
        ) -> None:
            """Rebuild the components and re-render the wizard message if bound."""

            self._configure_components()
            question = self._question()
            content = self.controller.build_panel_content(self.thread_id, self.step)
            content = self._apply_requirement_suffix(content, question)
            show_status = False
            if question is not None:
                qtype = self._question_type(question).strip().lower()
                show_status = qtype.startswith("short") or qtype.startswith("number") or qtype.startswith("paragraph")
            if notice:
                self._status_override = notice
            status_text = self._status_text(question) if show_status else (notice or "")
            if status_text:
                content = f"{content}\n\n{status_text}" if content else status_text
            if interaction is not None:
                response = getattr(interaction, "response", None)
                if response is not None:
                    is_done = getattr(response, "is_done", None)
                    done = False
                    if callable(is_done):
                        try:
                            done = bool(is_done())
                        except Exception:
                            done = False
                    elif isinstance(is_done, bool):
                        done = is_done
                    if not done:
                        try:
                            await interaction.response.edit_message(content=content, view=self)
                            return
                        except Exception:
                            log.warning("failed to edit wizard message via interaction", exc_info=True)
            if self._message is not None:
                try:
                    await self._message.edit(content=content, view=self)
                except Exception:
                    # Editing failures are non-fatal; keep the view state for retries.
                    pass
            if touch:
                self._touch()

        def stop(self) -> None:  # pragma: no cover - network
            self._cancel_idle_task()
            super().stop()

        def _touch(self) -> None:
            self._last_touch = time.monotonic()
            self._schedule_idle_refresh()

        def _cancel_idle_task(self) -> None:
            task = self._idle_task
            if task is None:
                return
            self._idle_task = None
            if not task.done():
                task.cancel()

        def _schedule_idle_refresh(self) -> None:
            self._cancel_idle_task()
            delay = max(0.0, (self._last_touch + self._IDLE_REFRESH_SECONDS) - time.monotonic())

            async def _runner() -> None:
                try:
                    if delay > 0:
                        await asyncio.sleep(delay)
                    await self._auto_refresh(notice=self._NOTICE_IDLE, reason="idle")
                except asyncio.CancelledError:  # pragma: no cover - lifecycle cleanup
                    return

            loop = asyncio.get_running_loop()
            self._idle_task = loop.create_task(_runner())

        async def _auto_refresh(self, *, notice: str, reason: str) -> None:
            if self._message is None:
                return
            if self._is_completed():
                log.debug(
                    "🛈 welcome_lifecycle — scope=%s • phase=idle_refresh_skipped • reason=completed",
                    getattr(self.controller, "flow", "welcome"),
                )
                return
            try:
                await self.refresh(notice=notice, touch=False)
            except Exception:
                log.warning("failed to auto-refresh onboarding wizard", exc_info=True)
                return
            logs.human(
                "info",
                "onboarding.recover",
                reason=reason,
                sid=self._session_id(),
                step=self.step,
                resumed=True,
            )
            self._touch()

        async def on_timeout(self) -> None:  # pragma: no cover - network
            # timeout=None keeps the view alive, but discord.py still calls on_timeout
            # if the object is stopped manually. Maintain compatibility by clearing
            # the idle task and delegating to the parent implementation.
            self._cancel_idle_task()
            await super().on_timeout()

        def _session_id(self) -> str | None:
            resolver = getattr(self.controller, "get_session_id", None)
            if callable(resolver):
                try:
                    return resolver(self.thread_id)
                except Exception:
                    log.debug("failed to resolve session id via getter", exc_info=True)
            attribute = getattr(self.controller, "session_id", None)
            if callable(attribute):
                try:
                    return attribute(self.thread_id)
                except Exception:
                    log.debug("failed to resolve session id via callable attribute", exc_info=True)
            if isinstance(attribute, (str, int)):
                return str(attribute)
            if isinstance(attribute, dict):
                value = attribute.get(self.thread_id)
                if value is not None:
                    return str(value)
            return None

        def _is_completed(self) -> bool:
            checker = getattr(self.controller, "is_session_completed", None)
            if callable(checker):
                try:
                    return bool(checker(self.thread_id))
                except Exception:
                    log.debug("failed to resolve welcome completion state", exc_info=True)
            return False

        # --- Internals --------------------------------------------------------
        async def interaction_check(self, interaction: discord.Interaction) -> bool:  # pragma: no cover - network
            checker = getattr(self.controller, "check_interaction", None)
            if callable(checker):
                try:
                    allowed, _ = await checker(self.thread_id, interaction)
                except Exception:
                    log.warning("inline wizard gate check failed", exc_info=True)
                    return False
                return allowed
            return True

        def _questions(self) -> Sequence[Any]:
            questions_dict = getattr(self.controller, "questions_by_thread", {})
            if isinstance(questions_dict, dict):
                questions = questions_dict.get(self.thread_id) or []
                if isinstance(questions, Sequence):
                    return questions
            return []

        def _align_to_visible(self) -> None:
            resolver = getattr(self.controller, "resolve_step", None)
            if not callable(resolver):
                return
            direction = getattr(self, "_last_direction", 1) or 1
            resolved, _ = resolver(self.thread_id, self.step, direction=direction)
            if resolved is None:
                return
            self.step = resolved

        def _question(self) -> Any | None:
            questions = list(self._questions())
            if not questions:
                return None
            if self.step < 0:
                self.step = 0
            if self.step >= len(questions):
                self.step = len(questions) - 1
            if self.step < 0 or self.step >= len(questions):
                return None
            return questions[self.step]

        def _question_label(self, question: Any | None) -> str:
            if question is None:
                return ""
            if isinstance(question, dict):
                return str(question.get("label") or question.get("text") or "")
            return str(getattr(question, "label", ""))

        @staticmethod
        def _raw_question_type(question: Any | None) -> str:
            if question is None:
                return ""
            if isinstance(question, dict):
                value = question.get("type") or question.get("qtype") or question.get("kind")
            else:
                value = getattr(question, "type", None)
                if not value:
                    value = getattr(question, "qtype", None)
            return str(value or "")

        def _question_type(self, question: Any | None) -> str:
            value = self._raw_question_type(question)
            return value

        def _question_required(self, question: Any | None) -> bool:
            if question is None:
                return False
            if isinstance(question, dict):
                return bool(question.get("required"))
            return bool(getattr(question, "required", False))

        def _apply_requirement_suffix(self, content: str, question: Any | None) -> str:
            """Append a consistent required/optional suffix for the current question."""
            if not question:
                return content

            required = getattr(question, "required", False)
            if isinstance(question, dict):
                required = question.get("required", required)
            required = bool(required)
            suffix = "Input is required" if required else "Input is optional"

            # Content is usually like: "Onboarding • {current}/{total}"
            # We always append the requirement, even if something already added a count.
            if suffix in content:
                return content

            # Avoid double spaces / dots at the end
            content = content.rstrip(" .")
            return f"{content} • {suffix}"

        @staticmethod
        def _note_tokens(note: Any) -> list[str]:
            if note is None:
                return []
            if isinstance(note, (list, tuple, set)):
                return [str(item).strip() for item in note if str(item).strip()]
            pieces = []
            for chunk in str(note).replace("\n", ",").split(","):
                token = chunk.strip()
                if token:
                    pieces.append(token)
            return pieces

        def _question_options(self, question: Any | None) -> Sequence[Any]:
            if question is None:
                return []
            qtype = self._raw_question_type(question).strip().lower()
            raw: Any
            if isinstance(question, dict):
                raw = question.get("options") or question.get("choices")
            else:
                raw = getattr(question, "options", None)
            if isinstance(raw, Sequence) and raw:
                return raw
            if not qtype.startswith("single-select") and not qtype.startswith("multi-select"):
                return []
            note = getattr(question, "note", None) if not isinstance(question, dict) else question.get("note")
            tokens = self._note_tokens(note)
            if tokens:
                return [{"label": token, "value": token} for token in tokens]
            validate = getattr(question, "validate", None) if not isinstance(question, dict) else question.get("validate")
            validate_tokens = self._note_tokens(validate)
            if validate_tokens:
                return [{"label": token, "value": token} for token in validate_tokens]
            return []

        def _is_multi_select(self, question: Any | None) -> bool:
            if question is None:
                return False
            qtype = self._raw_question_type(question).strip().lower()
            if qtype.startswith("multi-select"):
                return True
            multi_max = getattr(question, "multi_max", None)
            if multi_max:
                try:
                    return int(multi_max) > 1
                except (TypeError, ValueError):
                    return False
            if isinstance(question, dict):
                try:
                    return int(question.get("multi_max") or 0) > 1
                except (TypeError, ValueError):
                    return False
            hinted = getattr(question, "qtype", None)
            if isinstance(hinted, str) and hinted.strip().lower().startswith("multi-select"):
                return True
            return False

        def _question_key(self, question: Any | None) -> str:
            key_getter = getattr(self.controller, "_question_key", None)
            if callable(key_getter):
                return key_getter(question)  # type: ignore[arg-type]
            if isinstance(question, dict):
                value = question.get("id") or question.get("qid")
                return str(value or "")
            return str(getattr(question, "qid", ""))

        def _current_answer(self, question: Any | None) -> Any:
            key = self._question_key(question)
            accessor = getattr(self.controller, "_answer_for", None)
            if callable(accessor) and key:
                try:
                    return accessor(self.thread_id, key)
                except Exception:
                    log.debug("failed to read stored answer", exc_info=True)
            answers = getattr(self.controller, "answers_by_thread", {})
            if isinstance(answers, dict):
                thread_answers = answers.get(self.thread_id, {})
                if isinstance(thread_answers, dict):
                    return thread_answers.get(key)
            return None

        def _selected_tokens(self, stored: Any) -> set[str]:
            if stored is None:
                return set()
            tokens: set[str] = set()
            if isinstance(stored, dict):
                value = stored.get("value")
                if value:
                    tokens.add(str(value))
                values = stored.get("values")
                if isinstance(values, Iterable):
                    for item in values:
                        if isinstance(item, dict):
                            token = item.get("value") or item.get("label")
                            if token:
                                tokens.add(str(token))
                        elif item:
                            tokens.add(str(item))
                return tokens
            if isinstance(stored, Iterable) and not isinstance(stored, (str, bytes)):
                for item in stored:
                    if isinstance(item, dict):
                        token = item.get("value") or item.get("label")
                        if token:
                            tokens.add(str(token))
                    elif item:
                        tokens.add(str(item))
            return tokens

        def _text_default(self, question: Any | None) -> str:
            stored = self._current_answer(question)
            if stored is None:
                return ""
            if isinstance(stored, str):
                return stored
            if isinstance(stored, (int, float)):
                return str(stored)
            return ""

        def _has_current_answer(self, question: Any | None) -> bool:
            if question is None:
                return False
            checker = getattr(self.controller, "has_answer", None)
            if callable(checker):
                try:
                    return bool(checker(self.thread_id, question))
                except Exception:
                    log.debug("inline wizard answer check failed", exc_info=True)
                    return False
            stored = self._current_answer(question)
            if stored is None:
                return False
            if isinstance(stored, str):
                return bool(stored.strip())
            if isinstance(stored, (int, float)):
                return True
            if isinstance(stored, dict):
                return bool(stored)
            if isinstance(stored, Iterable):
                return any(True for _ in stored)
            return bool(stored)

        def _status_text(self, question: Any | None) -> str:
            if question is None:
                return ""
            key = self._question_key(question)
            if key != self._status_question_key:
                self._status_question_key = key
                self._status_error_hint = None
                self._status_override = None
            is_answered = self._has_current_answer(question)
            validation_error = self._status_error_hint
            return _status_for(
                is_answered,
                validation_error is None,
                validation_error,
                override=self._status_override,
            )

        def _configure_components(self) -> None:
            """Compose the interactive controls for the current wizard step."""

            self.clear_items()
            self._align_to_visible()
            question = self._question()
            if question is None:
                self.add_item(self.CancelButton(self))
                return
            options = list(self._question_options(question))
            qtype = self._question_type(question).strip().lower()
            if qtype == "bool":
                self.add_item(self.BoolButton(self, question, True))
                self.add_item(self.BoolButton(self, question, False))
            elif options:
                select = self.OptionSelect(self, question, options, self._is_multi_select(question))
                self.add_item(select)
            self.add_item(self.BackButton(self))
            has_answer = self._has_current_answer(question)
            self.add_item(self.NextButton(self, question, has_answer))
            self.add_item(self.RefreshButton(self))
            self.add_item(self.CancelButton(self))

        def is_last_step(self) -> bool:
            questions = self._questions()
            return self.step >= len(questions) - 1

        async def on_timeout(self) -> None:  # pragma: no cover - network
            for child in self.children:
                if hasattr(child, "disabled"):
                    child.disabled = True  # type: ignore[assignment]
            if self._message is not None:
                try:
                    await self._message.edit(view=self)
                except Exception:
                    log.warning("failed to disable wizard on timeout", exc_info=True)
            self.stop()

        class OptionSelect(discord.ui.Select):
            def __init__(
                self,
                parent: "OnboardWizard",
                question: Any,
                options: Sequence[Any],
                is_multi: bool,
            ) -> None:
                self._wizard = parent
                self.question = question
                stored = parent._current_answer(question)
                selected = parent._selected_tokens(stored)
                select_options: list[discord.SelectOption] = []
                for option in options:
                    if isinstance(option, dict):
                        label = str(option.get("label") or option.get("value") or "")
                        value = str(option.get("value") or option.get("label") or label)
                    else:
                        label = str(getattr(option, "label", ""))
                        value = str(getattr(option, "value", label))
                    select_options.append(
                        discord.SelectOption(label=label, value=value, default=value in selected)
                    )
                placeholder = parent._question_label(question) or "Select an option"
                max_values = 1
                if is_multi:
                    try:
                        configured = getattr(question, "multi_max", None)
                        if isinstance(question, dict):
                            configured = question.get("multi_max", configured)
                        if configured:
                            max_values = max(1, int(configured))
                        else:
                            max_values = max(1, len(select_options))
                    except (TypeError, ValueError):
                        max_values = max(1, len(select_options))
                super().__init__(
                    placeholder=placeholder,
                    min_values=0,
                    max_values=max_values,
                    options=select_options,
                )

            async def callback(self, interaction: discord.Interaction) -> None:
                await self._wizard._handle_select(interaction, self.question, list(self.values))
                self._wizard._touch()

        class BoolButton(discord.ui.Button):
            def __init__(self, parent: "OnboardWizard", question: Any, value: bool) -> None:
                label = "Yes" if value else "No"
                style = discord.ButtonStyle.success if value else discord.ButtonStyle.danger
                super().__init__(style=style, label=label)
                self._wizard = parent
                self.question = question
                self._value = value

            async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
                wizard = self._wizard
                token = "yes" if self._value else "no"
                key = wizard._question_key(self.question)
                await wizard.controller.set_answer(wizard.thread_id, key, token)
                wizard._status_error_hint = None
                wizard._status_override = None
                wizard._last_direction = 1
                await wizard.refresh(interaction)
                wizard._touch()

        class BackButton(discord.ui.Button):
            def __init__(self, parent: "OnboardWizard") -> None:
                super().__init__(style=discord.ButtonStyle.secondary, label="Back")
                self._wizard = parent
                if parent.step <= 0:
                    self.disabled = True

            async def callback(self, interaction: discord.Interaction) -> None:
                wizard = self._wizard
                wizard._last_direction = -1
                resolver = getattr(wizard.controller, "previous_visible_step", None)
                if callable(resolver):
                    previous = resolver(wizard.thread_id, wizard.step)
                    if previous is not None:
                        sync_step = getattr(wizard.controller, "_set_current_step_for_thread", None)
                        if callable(sync_step):
                            sync_step(wizard.thread_id, previous)
                        wizard.step = previous
                    elif wizard.step > 0:
                        wizard.step -= 1
                elif wizard.step > 0:
                    wizard.step -= 1
                await wizard.refresh(interaction)
                wizard._touch()

        class NextButton(discord.ui.Button):
            def __init__(self, parent: "OnboardWizard", question: Any, has_answer: bool) -> None:
                label = "Finish" if parent.is_last_step() else "Next"
                super().__init__(style=discord.ButtonStyle.primary, label=label)
                self._wizard = parent
                required = parent._question_required(question)
                # Disable Next only when required questions lack an answer.
                self.disabled = required and not has_answer

            async def callback(self, interaction: discord.Interaction) -> None:
                wizard = self._wizard
                wizard._last_direction = 1
                resolver = getattr(wizard.controller, "next_visible_step", None)
                if callable(resolver):
                    next_index = resolver(wizard.thread_id, wizard.step)
                else:
                    questions = wizard._questions()
                    next_index = wizard.step + 1 if wizard.step + 1 < len(questions) else None
                if next_index is None:
                    await wizard.controller.finish_inline_wizard(
                        wizard.thread_id,
                        interaction,
                        message=wizard._message,
                    )
                    wizard.stop()
                    return
                sync_step = getattr(wizard.controller, "_set_current_step_for_thread", None)
                if callable(sync_step):
                    sync_step(wizard.thread_id, next_index)
                wizard.step = next_index
                await wizard.refresh(interaction)
                wizard._touch()

        class CancelButton(discord.ui.Button):
            def __init__(self, parent: "OnboardWizard") -> None:
                super().__init__(style=discord.ButtonStyle.danger, label="Cancel")
                self._wizard = parent

            async def callback(self, interaction: discord.Interaction) -> None:
                wizard = self._wizard
                answers = getattr(wizard.controller, "answers_by_thread", None)
                if isinstance(answers, dict):
                    answers.pop(wizard.thread_id, None)
                notice = "❌ Wizard cancelled."
                response = getattr(interaction, "response", None)
                handled = False
                if response is not None and not response.is_done():
                    try:
                        await interaction.response.edit_message(content=notice, view=None)
                        handled = True
                    except Exception:
                        handled = False
                if not handled and wizard._message is not None:
                    try:
                        await wizard._message.edit(content=notice, view=None)
                    except Exception:
                        log.warning("failed to update wizard cancel message", exc_info=True)
                wizard.stop()

        class RefreshButton(discord.ui.Button):
            def __init__(self, parent: "OnboardWizard") -> None:
                super().__init__(style=discord.ButtonStyle.secondary, label="Refresh")
                self._wizard = parent

            async def callback(self, interaction: discord.Interaction) -> None:
                await self._wizard._handle_refresh(interaction)

        async def _handle_refresh(self, interaction: discord.Interaction | None) -> None:
            try:
                await self.refresh(interaction, notice=self._NOTICE_REFRESH, touch=False)
            finally:
                self._touch()
            logs.human(
                "info",
                "onboarding.recover",
                reason="user_refresh",
                sid=self._session_id(),
                step=self.step,
                resumed=True,
            )

        async def _handle_select(
            self,
            interaction: discord.Interaction,
            question: Any,
            selections: list[str],
        ) -> None:
            key = self._question_key(question)
            if not selections:
                await self.controller.set_answer(self.thread_id, key, None)
                self._status_error_hint = None
                self._status_override = "✅ Answer cleared."
                await self.refresh(interaction)
                return
            options: dict[str, str] = {}
            for option in self._question_options(question):
                if isinstance(option, dict):
                    label = str(option.get("label") or option.get("value") or "")
                    value = str(option.get("value") or option.get("label") or label)
                else:
                    label = str(getattr(option, "label", ""))
                    value = str(getattr(option, "value", label))
                options[value] = label
            if self._is_multi_select(question):
                stored: list[dict[str, str]] = []
                for token in selections:
                    label = options.get(token, token)
                    stored.append({"value": token, "label": label})
                await self.controller.set_answer(self.thread_id, key, stored)
            else:
                token = selections[0]
                label = options.get(token, token)
                await self.controller.set_answer(
                    self.thread_id,
                    key,
                    {"value": token, "label": label},
                )
            self._status_error_hint = None
            self._status_override = None
            await self.refresh(interaction)


OnboardWizard = OpenQuestionsPanelView.OnboardWizard


class WelcomePanel(discord.ui.View):
    """Minimal persistent panel for onboarding wizard interactions."""

    def __init__(self, controller, *, timeout: float | None = None) -> None:
        super().__init__(timeout=timeout)
        self.controller = controller
        self.log = getattr(controller, "log", None) if controller is not None else None

    def _resolve_controller(self, interaction: discord.Interaction):
        if self.controller is not None:
            return self.controller
        client = getattr(interaction, "client", None)
        state = getattr(client, "state", None) if client is not None else None
        getter = getattr(state, "get", None)
        if callable(getter):
            controller = getter("onboarding_controller")
            if controller is not None:
                self.controller = controller
                self.log = getattr(controller, "log", None)
                return controller
        if isinstance(state, dict):
            controller = state.get("onboarding_controller")
            if controller is not None:
                self.controller = controller
                self.log = getattr(controller, "log", None)
                return controller
        fallback = getattr(client, "onboarding_controller", None)
        if fallback is not None:
            self.controller = fallback
            self.log = getattr(fallback, "log", None)
            return fallback
        return None

    @discord.ui.button(label="Open questions", style=discord.ButtonStyle.primary, custom_id="open_questions")
    async def open_questions(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        try:
            await interaction.response.defer_update()
        except discord.InteractionResponded:
            pass
        except Exception:
            await _ensure_deferred(interaction)

        controller = self._resolve_controller(interaction)
        if controller is None:
            return

        await controller.launch(interaction)
        if self.log is not None:
            try:
                self.log.info(
                    "wizard:first_click",
                    extra={"panel_message_id": getattr(interaction.message, "id", None)},
                )
            except Exception:
                pass

    @discord.ui.button(label="Resume", style=discord.ButtonStyle.secondary, custom_id="resume_wizard")
    async def resume(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        try:
            await interaction.response.defer_update()
        except discord.InteractionResponded:
            pass
        except Exception:
            await _ensure_deferred(interaction)

        controller = self._resolve_controller(interaction)
        if controller is None:
            return

        thread_id = getattr(interaction.channel, "id", None)
        user_id = getattr(interaction.user, "id", None)
        existing = None
        if thread_id is not None and user_id is not None:
            try:
                from modules.onboarding.sessions import Session

                existing = Session.load_from_sheet(user_id, thread_id)
            except Exception:
                existing = None
        if existing is None:
            try:
                await interaction.followup.send(
                    "No saved onboarding session found — starting a new one.",
                    ephemeral=True,
                )
            except Exception:
                pass

        await controller.launch(interaction)

    @discord.ui.button(label="Restart", style=discord.ButtonStyle.secondary, custom_id="restart_wizard")
    async def restart(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        try:
            await interaction.response.defer_update()
        except discord.InteractionResponded:
            pass
        except Exception:
            await _ensure_deferred(interaction)

        controller = self._resolve_controller(interaction)
        if controller is None:
            return
        await controller.restart(interaction)


def register_views(bot: commands.Bot) -> None:
    """Register persistent onboarding views once the bot is ready."""

    state = getattr(bot, "state", None)
    controller = None
    getter = getattr(state, "get", None)
    if callable(getter):
        controller = getter("onboarding_controller")
    elif isinstance(state, dict):
        controller = state.get("onboarding_controller")
    if controller is None:
        controller = getattr(bot, "onboarding_controller", None)

    bot.add_view(WelcomePanel(controller=controller))
    if hasattr(bot, "logger"):
        try:
            bot.logger.info("onboarding: persistent WelcomePanel view registered")
        except Exception:
            pass
