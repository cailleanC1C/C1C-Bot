from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import signal
import time
from typing import Optional

import discord
from discord.ext import commands

from shared.config import (
    get_env_name,
    get_allowed_guild_ids,
    is_guild_allowed,
    get_config_snapshot,
)
from shared.logfmt import LogTemplates, guild_label, user_label, human_reason
from shared.redaction import sanitize_text
from shared import health as healthmod
from shared import socket_heartbeat as hb
from modules.common.runtime import Runtime
from modules.common import keepalive
from modules.coreops import ready as core_ready
from c1c_coreops.config import (
    build_command_variants,
    build_lookup_sequence,
    load_coreops_settings,
    normalize_command_text,
)
from c1c_coreops.prefix import detect_admin_bang_command
from c1c_coreops.rbac import (
    get_admin_role_ids,
    get_staff_role_ids,
    is_admin_member,
)
from c1c_coreops.cron_summary import emit_daily_summary
from modules.recruitment.reporting.daily_recruiter_update import ensure_scheduler_started

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("c1c.app")

LOG_MESSAGE_CONTENT_MAX_LEN = 200

INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.members = True

BANG_PREFIX = "!"
COREOPS_SETTINGS = load_coreops_settings()
COREOPS_COMMANDS = tuple(COREOPS_SETTINGS.admin_bang_base_commands)
COREOPS_ADMIN_ALLOWLIST = {
    normalize_command_text(item) for item in COREOPS_SETTINGS.admin_bang_allowlist
}

bot = commands.Bot(
    command_prefix=commands.when_mentioned_or(BANG_PREFIX),
    intents=INTENTS,
)
bot.remove_command("help")

runtime: Runtime
_shutdown_lock: asyncio.Lock | None = None
_shutdown_started = False


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


LOG_MESSAGE_CONTENT = _env_bool("LOG_MESSAGE_CONTENT", False)


def _truncate_text(text: str, max_len: int = LOG_MESSAGE_CONTENT_MAX_LEN) -> str:
    if len(text) <= max_len:
        return text
    if max_len <= 1:
        return text[:max_len]
    return f"{text[: max_len - 1]}…"


def _safe_logged_message_content(content: str) -> str:
    sanitized = sanitize_text(content)
    return _truncate_text(str(sanitized), LOG_MESSAGE_CONTENT_MAX_LEN)


def _can_dispatch_bare_coreops(member: discord.abc.User | discord.Member | None) -> bool:
    if not isinstance(member, discord.Member):
        return False
    return is_admin_member(member)


def _extract_bang_query(content: str) -> str | None:
    raw = (content or "").strip()
    if not raw.startswith("!"):
        return None
    trimmed = raw[1:].lstrip()
    if not trimmed:
        return None
    parts = trimmed.split(None, 1)
    if len(parts) < 2:
        return None
    remainder = parts[1].strip()
    return remainder if remainder else None


def _normalize_admin_invocation(base: str, remainder: str | None) -> str:
    parts = [base]
    if remainder:
        parts.append(remainder)
    return normalize_command_text(" ".join(parts))


async def _maybe_capture_onboarding_answer(message: discord.Message) -> bool:
    channel = getattr(message, "channel", None)
    if not isinstance(channel, discord.Thread):
        return False
    try:
        thread_id = int(getattr(channel, "id", 0))
    except (TypeError, ValueError):
        return False

    try:
        from modules.onboarding.ui import panels
    except Exception:
        return False

    controller = panels.get_controller(thread_id)
    if controller is None:
        return False

    handler = getattr(controller, "handle_thread_message", None)
    if not callable(handler):
        return False

    try:
        return bool(await handler(message))
    except Exception:
        log.warning("onboarding auto-capture failed", exc_info=True)
        return False


def _resolve_coreops_command(lookup: str) -> commands.Command | None:
    for candidate in build_command_variants(COREOPS_SETTINGS, lookup):
        command = bot.get_command(candidate)
        if command is not None:
            return command
    return None


def _extract_mention_invocation(
    message: discord.Message,
) -> tuple[str, str | None] | None:
    if not bot.user:
        return None
    content = message.content or ""
    mention_variants = (f"<@{bot.user.id}>", f"<@!{bot.user.id}>")
    lowered = content.lower()
    for variant in mention_variants:
        if lowered.startswith(variant.lower()):
            remainder = content[len(variant) :].strip()
            if not remainder:
                return None
            parts = remainder.split(None, 1)
            command = parts[0].strip().lower()
            query = parts[1].strip() if len(parts) > 1 else None
            return command, query
    return None


async def _enforce_guild_allow_list(
    *, log_when_empty: bool = False, log_success: bool = True
) -> bool:
    allowed_guilds = get_allowed_guild_ids()
    allowed_sorted = sorted(allowed_guilds)
    connected_guilds = list(bot.guilds)
    allowed_labels = [guild_label(bot, gid) for gid in allowed_sorted] if allowed_sorted else []
    connected_labels = [guild_label(bot, g.id) for g in connected_guilds]
    if not allowed_guilds:
        if log_when_empty:
            log.warning("Guild allow-list empty; gating disabled")
            message = LogTemplates.allowlist(
                allowed=allowed_labels,
                connected=connected_labels,
                ok=False,
            )
            await runtime.send_log_message(f"{message} • gating=disabled")
        return True

    unauthorized = [g for g in connected_guilds if not is_guild_allowed(g.id)]
    if unauthorized:
        names = ", ".join(guild_label(bot, g.id) for g in unauthorized)
        log.error(
            "Guild allow-list violation: %s. allowed=%s",
            names,
            allowed_sorted,
        )
        try:
            violation = LogTemplates.allowlist_violation(
                allowed=allowed_labels,
                offending=[guild_label(bot, g.id) for g in unauthorized],
            )
            await runtime.send_log_message(violation)
            await bot.close()
        finally:
            return False

    if log_success:
        log.info(
            "Guild allow-list verified",
            extra={
                "allowed": allowed_sorted,
                "connected": [g.id for g in connected_guilds],
            },
        )
        message = LogTemplates.allowlist(
            allowed=allowed_labels,
            connected=connected_labels,
            ok=True,
        )
        await runtime.send_log_message(message)
    return True


@bot.event
async def on_ready():
    hb.note_ready()
    healthmod.set_component("discord", True)
    log.info(
        'Bot ready as %s | env=%s | prefixes=["%s", "@mention"]',
        bot.user,
        get_env_name(),
        BANG_PREFIX,
    )
    log.info(
        "CoreOps RBAC: admin_role_ids=%s staff_role_ids=%s",
        sorted(get_admin_role_ids()),
        sorted(get_staff_role_ids()),
        )
    bot._c1c_started_mono = _STARTED_MONO

    if not await _enforce_guild_allow_list(log_when_empty=True):
        return

    started, interval, stall, grace = runtime.watchdog(delay_sec=5.0)
    if started:
        async def announce() -> None:
            await asyncio.sleep(5.0)
            await runtime.send_log_message(
                LogTemplates.watchdog(
                    interval_s=interval,
                    stall_s=stall,
                    disconnect_grace_s=grace,
                )
            )

        runtime.scheduler.spawn(announce(), name="watchdog_announce")

    if not hasattr(bot, "_cron_summary_task"):
        async def _daily_summary_loop() -> None:
            while True:
                now = dt.datetime.now(dt.timezone.utc)
                target = now.replace(hour=0, minute=5, second=0, microsecond=0)
                if now >= target:
                    target = target + dt.timedelta(days=1)
                await asyncio.sleep((target - now).total_seconds())
                try:
                    await emit_daily_summary(CRON_JOB_NAMES)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.warning("[cron] summary_failed", exc_info=True)

        runtime.scheduler.spawn(_daily_summary_loop(), name="cron_daily_summary")
        bot._cron_summary_task = True
        log.info("[cron] summary scheduler started (00:05Z)")

    await keepalive.ensure_started(bot)

    runtime.schedule_startup_preload()

    await ensure_scheduler_started(bot)

    await core_ready.on_ready(bot)


@bot.event
async def on_connect():
    hb.note_connected()


@bot.event
async def on_resumed():
    hb.note_connected()


@bot.event
async def on_error(event: str, *_args, **_kwargs) -> None:
    log.exception("Unhandled exception in %s", event)


@bot.event
async def on_socket_response(_payload):
    hb.touch()


try:
    @bot.event
    async def on_socket_raw_receive(_):
        hb.touch()
except Exception:
    pass


@bot.event
async def on_disconnect():
    hb.note_disconnected()
    healthmod.set_component("discord", False)


@bot.event
async def on_guild_join(_guild: discord.Guild):
    hb.touch()
    await _enforce_guild_allow_list(log_success=False)


@bot.event
async def on_message(message: discord.Message):
    hb.touch()
    if bot.user and message.author.id == bot.user.id:
        return

    if await _maybe_capture_onboarding_answer(message):
        return

    content = message.content or ""
    attachments_count = len(getattr(message, "attachments", []) or [])
    base_fields = (
        getattr(message.guild, "id", None),
        getattr(message.channel, "id", None),
        getattr(message, "id", None),
        getattr(getattr(message, "author", None), "id", None),
        len(content),
        attachments_count,
    )
    if LOG_MESSAGE_CONTENT:
        safe_content = _safe_logged_message_content(content)
        log.info(
            "seen msg: guild=%s chan=%s msg=%s author=%s content_len=%s attachments=%s content=%r",
            *base_fields,
            safe_content,
        )
    else:
        log.info(
            "seen msg: guild=%s chan=%s msg=%s author=%s content_len=%s attachments=%s",
            *base_fields,
        )

    content = content.strip()

    mention_invocation = _extract_mention_invocation(message)
    if mention_invocation:
        command_name, remainder = mention_invocation
        ctx = await bot.get_context(message)
        if command_name == "help":
            cog = bot.get_cog("CoreOpsCog")
            if cog is not None and hasattr(cog, "render_help"):
                ops_help_command = bot.get_command("ops help")
                if ops_help_command is not None:
                    ctx.command = ops_help_command
                    ctx.invoked_with = "help"
                await cog.render_help(ctx, query=remainder)
            return
        if command_name == "ping":
            ops_ping_command = bot.get_command("ops ping")
            if ops_ping_command is not None:
                ctx.command = ops_ping_command
                ctx.invoked_with = "ping"
                await bot.invoke(ctx)
            else:
                await ctx.send(str(sanitize_text("Ping command unavailable.")))
            return

    cmd_name = detect_admin_bang_command(
        message, commands=COREOPS_COMMANDS, is_admin=_can_dispatch_bare_coreops
    )
    if cmd_name:
        remainder = _extract_bang_query(message.content or "")
        normalized = _normalize_admin_invocation(cmd_name, remainder)
        base_name = normalize_command_text(cmd_name)
        if (
            normalized not in COREOPS_ADMIN_ALLOWLIST
            and base_name not in COREOPS_ADMIN_ALLOWLIST
        ):
            return

        ctx = await bot.get_context(message)
        if base_name == "help":
            cog = bot.get_cog("CoreOpsCog")
            if cog is not None and hasattr(cog, "render_help"):
                ops_help_command = bot.get_command("ops help")
                if ops_help_command is not None:
                    ctx.command = ops_help_command
                    ctx.invoked_with = "help"
                await cog.render_help(ctx, query=remainder)
            return

        for lookup in build_lookup_sequence(cmd_name, remainder):
            command = _resolve_coreops_command(lookup)
            if command is not None:
                ctx.command = command
                ctx.invoked_with = command.qualified_name
                await bot.invoke(ctx)
                return
        return

    await bot.process_commands(message)


@bot.event
async def on_command_error(ctx: commands.Context, error: Exception):
    log.warning(
        "cmd error: cmd=%s user=%s err=%r",
        getattr(ctx.command, "name", None),
        getattr(ctx.author, "id", None),
        error,
    )
    try:
        await runtime.send_log_message(
            LogTemplates.cmd_error(
                command=getattr(ctx.command, "name", None) or "-",
                user=user_label(getattr(ctx, "guild", None), getattr(ctx.author, "id", None)),
                reason=human_reason(error),
            )
        )
    except Exception:
        log.exception("failed to send command error to log channel")


BOT_VERSION = os.getenv("BOT_VERSION", "dev")
_STARTED_MONO = time.monotonic()


_RUNTIME_EVENT_NAMES = (
    "on_ready",
    "on_connect",
    "on_resumed",
    "on_error",
    "on_socket_response",
    "on_socket_raw_receive",
    "on_disconnect",
    "on_guild_join",
    "on_message",
    "on_command_error",
)


def _clone_bot_for_retry() -> commands.Bot:
    cloned = commands.Bot(
        command_prefix=commands.when_mentioned_or(BANG_PREFIX),
        intents=INTENTS,
    )
    cloned.remove_command("help")
    for event_name in _RUNTIME_EVENT_NAMES:
        handler = getattr(bot, event_name, None)
        if handler is None:
            continue
        setattr(cloned, event_name, handler)
    return cloned


def _set_active_bot_for_runtime(rebuilt_bot: commands.Bot) -> None:
    global bot
    bot = rebuilt_bot


runtime = Runtime(
    bot,
    bot_factory=_clone_bot_for_retry,
    bot_rebuild_hook=_set_active_bot_for_runtime,
)


def uptime_seconds() -> float:
    return max(0.0, time.monotonic() - _STARTED_MONO)


def latency_seconds(bot: commands.Bot) -> Optional[float]:
    try:
        return float(getattr(bot, "latency", None)) if bot.latency is not None else None
    except Exception:
        return None


CONFIG_META = {
    "source": "shared.config",
    "status": "ok",
    "loaded_at": None,
    "last_error": None,
}
CFG = get_config_snapshot()


async def main() -> None:
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN not set")
    try:
        loop = asyncio.get_running_loop()
        global _shutdown_lock
        _shutdown_lock = asyncio.Lock()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(
                    sig, lambda s=sig: asyncio.create_task(_shutdown(f"signal:{s.name}"))
                )
            except NotImplementedError:
                log.warning("Signal handlers not supported", extra={"signal": sig.name})
        await runtime.start(token)
    finally:
        await _shutdown("runtime_exit")


async def _shutdown(reason: str) -> None:
    global _shutdown_started, _shutdown_lock
    if _shutdown_lock is None:
        _shutdown_lock = asyncio.Lock()
    async with _shutdown_lock:
        if _shutdown_started:
            return
        _shutdown_started = True
    log.info("Shutdown requested", extra={"reason": reason})
    try:
        if not bot.is_closed():
            await bot.close()
    except Exception:
        log.exception("Bot shutdown failed")
    try:
        await runtime.close()
    except Exception:
        log.exception("Runtime shutdown failed")


if __name__ == "__main__":
    asyncio.run(main())
