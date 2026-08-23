from .models import ResetReminder
from .views import ResetReminderView
from . import scheduler as _scheduler
from .panel_rollover import install_reset_reminder_panel_rollover

install_reset_reminder_panel_rollover(_scheduler)

process_reset_reminders = _scheduler.process_reset_reminders
register_persistent_reset_views = _scheduler.register_persistent_reset_views
schedule_reset_reminder_jobs = _scheduler.schedule_reset_reminder_jobs

__all__ = [
    "ResetReminder",
    "ResetReminderView",
    "process_reset_reminders",
    "register_persistent_reset_views",
    "schedule_reset_reminder_jobs",
]
