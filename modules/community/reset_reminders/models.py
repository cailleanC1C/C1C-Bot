from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(frozen=True, slots=True)
class ResetReminder:
    reset_id: str
    label: str
    status: str
    reference_date_utc: datetime
    cycle_days: int
    lead_minutes: int
    role_id: int
    channel_id: int
    thread_id: Optional[int]
    embed_title: str
    embed_description: str
    embed_footer: str
    button_label_opt_in: str
    button_label_opt_out: str
    last_sent_for_reset_utc: Optional[datetime]
    last_message_id: Optional[int]
