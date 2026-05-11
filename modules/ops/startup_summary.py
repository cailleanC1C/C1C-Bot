"""Discord admin startup summary renderer."""
from __future__ import annotations

import datetime as dt
import logging
from typing import Iterable

from discord.ext import commands

from modules.common.runtime import Runtime
from modules.onboarding.watcher_welcome import _channel_readable_label
from shared.cache import telemetry as cache_telemetry
from shared.config import cfg_int
from shared.sheet_config import shared_config

log = logging.getLogger("c1c.ops.startup_summary")

_CACHE_BUCKETS = ["clan_tags", "clans", "fusion", "fusion_events", "leagues", "onboarding_questions", "reaction_roles", "templates"]
_SCHEDULER_MAP = {
    "clans": "cache_refresh:clans",
    "templates": "cache_refresh:templates",
    "clan_tags": "cache_refresh:clan_tags",
    "onboarding_questions": "cache_refresh:onboarding_questions",
    "cleanup": "cleanup_watcher",
    "housekeeping_keepalive": "housekeeping_keepalive",
    "mirralith_overview": "mirralith_overview",
    "shard_weekly_reminders": "shard_weekly_reminders",
    "fusion_reminders": "fusion_reminders",
    "reset_reminders": "reset_reminders",
}

def _channel_line(bot_client: commands.Bot, channel_id: int | None) -> str:
    if not channel_id:
        return "not configured"
    mention = f"<#{channel_id}>"
    try:
        channel = bot_client.get_channel(int(channel_id))
    except Exception:
        channel = None
    if channel is None:
        return mention
    try:
        return f"{mention} ({_channel_readable_label(bot_client, channel_id).lstrip('#')})"
    except Exception:
        return mention

def _fmt_next(jobs: Iterable[object], name: str) -> str:
    for job in jobs:
        if getattr(job, 'name', None) != name:
            continue
        next_run = getattr(job, 'next_run', None)
        if next_run is None:
            return 'pending'
        try:
            return next_run.astimezone(dt.timezone.utc).replace(second=0, microsecond=0).strftime('%Y-%m-%d %H:%M UTC')
        except Exception:
            return 'pending'
    return 'not scheduled'

def render_startup_summary(*, bot_client: commands.Bot, runtime: Runtime, jobs: list[object]) -> str:
    lines = ["✅ Woadkeeper startup complete", ""]

    try:
        toggles = shared_config.features
        watchers = [
            "Watchers",
            f"• Promo watcher: {'enabled' if bool(getattr(toggles,'promo_watcher_enabled',False)) else 'disabled'} — {_channel_line(bot_client, cfg_int('PROMO_CHANNEL_ID', 0) or None)}",
            f"• Welcome watcher: {'enabled' if bool(getattr(toggles,'welcome_watcher_enabled',False)) else 'disabled'} — {_channel_line(bot_client, cfg_int('WELCOME_CHANNEL_ID', 0) or None)}",
        ]
        lines.extend(watchers)
    except Exception:
        log.exception('startup summary watchers section unavailable')
        lines.extend(["Watchers", "• unavailable"])

    try:
        cache_lines=["Cache"]
        for bucket in _CACHE_BUCKETS:
            try:
                snap = cache_telemetry.get_snapshot(bucket)
                raw = str((getattr(snap,'last_result',None) or 'pending')).lower()
                status = raw if raw in {'ok','pending','stale','error'} else ('ok' if raw in {'success','retry_ok'} else 'pending')
                count = getattr(snap,'item_count',None)
                cache_lines.append(f"• {bucket}: {status} ({count if count is not None else '?'})")
            except Exception:
                log.exception('startup summary cache item unavailable', extra={'bucket':bucket})
                cache_lines.append(f"• {bucket}: pending (?)")
        lines.extend(["",*cache_lines])
    except Exception:
        log.exception('startup summary cache section unavailable')
        lines.extend(["", "Cache", "• unavailable"])

    try:
        scheduler_lines=["Schedulers"]
        for label, job_name in _SCHEDULER_MAP.items():
            next_val = _fmt_next(jobs, job_name)
            if next_val == 'not scheduled':
                scheduler_lines.append(f"• {label}: not scheduled")
            else:
                scheduler_lines.append(f"• {label}: next {next_val}")
        lines.extend(["",*scheduler_lines])
    except Exception:
        log.exception('startup summary scheduler section unavailable')
        lines.extend(["", "Schedulers", "• unavailable"])

    try:
        interval = int(getattr(runtime, '_watchdog_check_sec', 300))
        stall = int(getattr(runtime, '_watchdog_stall_sec', 1200))
        grace = int(getattr(runtime, '_watchdog_disconnect_grace_sec', 6000))
        lines.extend(["", "Watchdog", f"• interval {interval}s", f"• stall {stall}s", f"• disconnect grace {grace}s"])
    except Exception:
        log.exception('startup summary watchdog section unavailable')
        lines.extend(["", "Watchdog", "• unavailable"])

    return "\n".join(lines)
