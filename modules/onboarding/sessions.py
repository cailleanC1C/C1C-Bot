"""Session management helpers for the onboarding wizard."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from shared.sheets import onboarding_sessions as sess_sheet

log = logging.getLogger(__name__)


def utc_now() -> datetime:
    """Return the current UTC timestamp."""

    return datetime.now(timezone.utc)


@dataclass
class Session:
    thread_id: int
    applicant_id: int
    thread_name: str = ""
    panel_message_id: int | None = None
    step_index: int = 0
    answers: Dict[str, object] = field(default_factory=dict)
    completed: bool = False
    completed_at: datetime | None = None
    empty_first_reminder_at: datetime | None = None
    empty_warning_sent_at: datetime | None = None
    first_reminder_at: datetime | None = None
    warning_sent_at: datetime | None = None
    auto_closed_at: datetime | None = None
    updated_at: datetime | None = None
    created_at: datetime = field(default_factory=utc_now)
    last_updated: datetime = field(init=False)

    def __post_init__(self) -> None:
        if self.updated_at is None:
            self.updated_at = self.created_at
        if self.updated_at.tzinfo is None:
            self.updated_at = self.updated_at.replace(tzinfo=timezone.utc)
        self.last_updated = self.updated_at

    def reset(self) -> None:
        """Reset the wizard state for the session."""

        self.step_index = 0
        self.answers.clear()
        self._touch()

    # PR-B: answer helpers
    def set_answer(self, gid: str, value) -> None:
        self.answers[gid] = value
        self._touch()

    def has_answer(self, gid: str) -> bool:
        return gid in self.answers and self.answers[gid] not in (None, "", "—", [])

    def get_answer(self, gid: str, default=None):
        return self.answers.get(gid, default)

    def mark_completed(self) -> None:
        self.completed = True
        self.completed_at = utc_now()
        self._touch()

    def _touch(self, *, timestamp: datetime | None = None) -> None:
        candidate = timestamp or utc_now()
        if candidate.tzinfo is None:
            candidate = candidate.replace(tzinfo=timezone.utc)
        self.updated_at = candidate
        self.last_updated = candidate

    # === Sheet persistence helpers ===
    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "applicant_id": self.applicant_id,
            "panel_message_id": self.panel_message_id,
            "step_index": self.step_index,
            "answers": dict(self.answers),
            "completed": self.completed,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "first_reminder_at": self.first_reminder_at.isoformat()
            if self.first_reminder_at or self.empty_first_reminder_at
            else None,
            "warning_sent_at": self.warning_sent_at.isoformat()
            if self.warning_sent_at or self.empty_warning_sent_at
            else None,
            "auto_closed_at": self.auto_closed_at.isoformat() if self.auto_closed_at else None,
        }

    def save_to_sheet(self) -> None:
        payload = {
            "user_id": str(self.applicant_id),
            "thread_id": str(self.thread_id),
            "thread_name": self.thread_name or "",
            "panel_message_id": int(self.panel_message_id or 0),
            "step_index": int(self.step_index),
            "answers": self.answers,
            "completed": bool(self.completed),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "updated_at": _serialize_dt(self.updated_at),
            "first_reminder_at": (self.first_reminder_at or self.empty_first_reminder_at).isoformat()
            if (self.first_reminder_at or self.empty_first_reminder_at)
            else None,
            "warning_sent_at": (self.warning_sent_at or self.empty_warning_sent_at).isoformat()
            if (self.warning_sent_at or self.empty_warning_sent_at)
            else None,
            "auto_closed_at": self.auto_closed_at.isoformat() if self.auto_closed_at else None,
        }
        sess_sheet.save(payload)

    async def asave_to_sheet(self) -> None:
        payload = {
            "user_id": str(self.applicant_id),
            "thread_id": str(self.thread_id),
            "thread_name": self.thread_name or "",
            "panel_message_id": int(self.panel_message_id or 0),
            "step_index": int(self.step_index),
            "answers": self.answers,
            "completed": bool(self.completed),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "updated_at": _serialize_dt(self.updated_at),
            "first_reminder_at": (self.first_reminder_at or self.empty_first_reminder_at).isoformat()
            if (self.first_reminder_at or self.empty_first_reminder_at)
            else None,
            "warning_sent_at": (self.warning_sent_at or self.empty_warning_sent_at).isoformat()
            if (self.warning_sent_at or self.empty_warning_sent_at)
            else None,
            "auto_closed_at": self.auto_closed_at.isoformat() if self.auto_closed_at else None,
        }
        await sess_sheet.asave(payload)

    @classmethod
    def load_from_sheet(cls, applicant_id: int, thread_id: int) -> Optional["Session"]:
        row = sess_sheet.load(int(applicant_id), int(thread_id))
        if not row:
            return None
        return cls.from_sheet_row(applicant_id=applicant_id, thread_id=thread_id, row=row)

    @classmethod
    def from_sheet_row(cls, applicant_id: int, thread_id: int, row: Dict[str, Any]) -> "Session":
        panel_id = row.get("panel_message_id") or None
        session = cls(
            thread_id=int(thread_id),
            applicant_id=int(applicant_id),
            panel_message_id=panel_id,
            thread_name=str(row.get("thread_name") or ""),
        )
        session.step_index = int(row.get("step_index", 0) or 0)
        answers = row.get("answers") or {}
        if isinstance(answers, dict):
            session.answers = dict(answers)
        else:
            session.answers = {}
        session.completed = bool(row.get("completed", False))
        completed_at = row.get("completed_at")
        if completed_at:
            try:
                normalized = str(completed_at).replace("Z", "+00:00")
                session.completed_at = datetime.fromisoformat(normalized)
            except Exception:
                session.completed_at = None
        reminder_token = row.get("first_reminder_at")
        reminder_at = _parse_iso(reminder_token) if reminder_token else None
        if reminder_at:
            session.first_reminder_at = reminder_at
            session.empty_first_reminder_at = reminder_at
        warning_token = row.get("warning_sent_at")
        warning_at = _parse_iso(warning_token) if warning_token else None
        if warning_at:
            session.warning_sent_at = warning_at
            session.empty_warning_sent_at = warning_at
        auto_closed = row.get("auto_closed_at")
        if auto_closed:
            session.auto_closed_at = _parse_iso(auto_closed)
        updated_at = row.get("updated_at")
        session.updated_at = _parse_iso(updated_at) or utc_now()
        session.last_updated = session.updated_at
        return session


def _parse_iso(value: Any) -> datetime | None:
    try:
        normalized = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def _serialize_dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    normalized = value
    if normalized.tzinfo is None:
        normalized = normalized.replace(tzinfo=timezone.utc)
    return normalized.isoformat()


class SessionStore:
    """In-memory store for onboarding wizard sessions."""

    def __init__(self) -> None:
        self._sessions: Dict[Tuple[int, int], Session] = {}

    async def load(self, thread_id: int, applicant_id: int) -> Session:
        key = (thread_id, applicant_id)
        session = self._sessions.get(key)
        if session is None:
            session = Session(thread_id=thread_id, applicant_id=applicant_id)
            self._sessions[key] = session
        return session

    async def save(self, session: Session) -> Session:
        key = (session.thread_id, session.applicant_id)
        self._sessions[key] = session
        session.last_updated = utc_now()
        return session


store = SessionStore()


async def ensure_session_for_thread(
    user_id: int,
    thread_id: int,
    *,
    updated_at: datetime | None = None,
    thread_name: str | None = None,
    create_if_missing: bool = True,
    preloaded_session_row: Dict[str, Any] | None = None,
) -> Session | None:
    """Load or create a session row for the given ``user_id`` and ``thread_id``.

    The helper preserves the earliest known ``updated_at`` timestamp when provided,
    while guaranteeing a single row per ``(user_id, thread_id)`` key.
    """

    def _normalize_ts(candidate: datetime | None) -> datetime | None:
        if candidate is None:
            return None
        if candidate.tzinfo is None:
            return candidate.replace(tzinfo=timezone.utc)
        return candidate.astimezone(timezone.utc)

    normalized_updated = _normalize_ts(updated_at)
    try:
        if preloaded_session_row is not None:
            existing = Session.from_sheet_row(user_id, thread_id, preloaded_session_row)
        else:
            row = await sess_sheet.aload(int(thread_id))
            existing = Session.from_sheet_row(user_id, thread_id, row) if row else None
    except Exception:
        log.exception(
            "failed to load onboarding session", extra={"thread_id": thread_id, "user_id": user_id}
        )
        existing = None

    if existing is not None:
        updated = False
        if thread_name and not existing.thread_name:
            existing.thread_name = thread_name
            updated = True
        if normalized_updated and (
            existing.updated_at is None
            or normalized_updated < _normalize_ts(existing.updated_at)
        ):
            existing.updated_at = normalized_updated
            updated = True
        if updated:
            try:
                await existing.asave_to_sheet()
            except Exception:
                log.exception(
                    "failed to persist onboarding session timestamp",
                    extra={"thread_id": thread_id, "user_id": user_id},
                )
        return existing

    if not create_if_missing:
        return None

    session = Session(
        thread_id=int(thread_id),
        applicant_id=int(user_id),
        thread_name=thread_name or "",
        updated_at=normalized_updated or utc_now(),
    )
    try:
        await session.asave_to_sheet()
    except Exception:
        log.exception(
            "failed to create onboarding session", extra={"thread_id": thread_id, "user_id": user_id}
        )
    return session


__all__ = ["Session", "SessionStore", "store", "utc_now", "ensure_session_for_thread"]
