# Watchers & Scheduled Jobs

## Purpose
Watchers keep the recruitment runtime “always-on” by reacting to Discord events,
refreshing caches, and nudging Render so the container never idles. This single
source of truth covers every automation hook:

- **Event-driven watchers** listen to welcome/promo threads and infrastructure
  events, writing to Sheets and keeping onboarding state aligned.
- **Scheduled jobs** preload caches, post the Daily Recruiter Update, and surface
  health telemetry so command handlers always operate against fresh data.
- **Keepalive** pings the public route to prevent Render from hibernating,
  complementing the watchdog thresholds.

## Watcher & job inventory
### Event-driven watchers
| Name | Location | Trigger | Responsibilities | Logging | Feature toggles / config |
| --- | --- | --- | --- | --- | --- |
| **Welcome watcher** | `modules.onboarding.watcher_welcome.WelcomeWatcher` | Ticket Tool greeting, 🎫 emoji, manual ticket close, Ticket Tool close message | Posts/reposts the onboarding questionnaire in the configured welcome channel, records answers into the onboarding Sheet, prompts for clan confirmation, reconciles reservations, and renames threads on closure. Also emits the onboarding lifecycle notice during startup. | `c1c.onboarding.welcome_watcher` logger with `✅/📘 Welcome watcher` startup lines (channel + channel_id) and `Welcome panel` lifecycle logs scoped to `WELCOME_CHANNEL_ID`. | Requires `WELCOME_CHANNEL_ID`, `WELCOME_TICKETS_TAB`, and FeatureToggles keys `welcome_enabled`, `enable_welcome_hook`, `welcome_dialog`, and `recruitment_welcome`. |
| **Promo watcher** | `modules.onboarding.watcher_promo.PromoTicketWatcher` | Promo ticket open + close events | Logs promo ticket lifecycle events to `PROMO_TICKETS_TAB`, maps R/M/L prefixes to type strings, attaches the Open Questions panel for promo triggers in the configured promo channel, and prompts for clan tag/progression on closure. | `c1c.onboarding.promo_watcher` logger with `✅ Promo watcher` startup entries plus `Promo panel` lifecycle logs (trigger + flow) scoped to `PROMO_CHANNEL_ID`. | Requires `PROMO_CHANNEL_ID` plus FeatureToggles keys `promo_enabled` and `enable_promo_hook`. |
| **League submission watcher** | `modules.community.leagues.cog.LeaguesCog` | Image attachments in `LEAGUES_SUBMISSION_CHANNEL_ID` | Grants `C1C_LEAGUE_ROLE_ID` on the first qualifying attachment so submitters are tagged for weekly announcements. | `c1c.community.leagues` info line on successful grants. | Requires `LEAGUES_SUBMISSION_CHANNEL_ID` and `C1C_LEAGUE_ROLE_ID`. |

### Scheduled jobs & loops
| Job | Module | Cadence | Responsibilities | Logging | Config / toggles |
| --- | --- | --- | --- | --- | --- |
| **Cache refresh – clans** | `modules.common.runtime.scheduler` (`shared.sheets.recruitment`) | Every 3 h | Clears the `clans` bucket and reloads recruitment roster data so `!clanmatch` and placements operate on fresh availability. | `[cache] bucket=clans` embeds plus structured console logs (success/error) routed to `LOG_CHANNEL_ID`. | `CLANS_TAB` sheet key; cadence is fixed in code today. |
| **Cache refresh – templates** | Same scheduler | Every 7 d | Refreshes welcome/promo template content, ensuring watchers post the latest copy. | `[cache] bucket=templates` logs. | `WELCOME_TEMPLATES_TAB` sheet key. |
| **Cache refresh – clan_tags** | Same scheduler | Every 7 d | Refreshes the clan tag autocomplete cache used in the watcher dropdowns. | `[cache] bucket=clan_tags` logs. | `CLAN_TAGS_CACHE_TTL_SEC` controls TTL; cadence fixed. |
| **Onboarding questions refresh** | `shared.sheets.onboarding` warmers | Weekly | Reloads onboarding question forms to match the latest Config worksheet. | `[cache] bucket=onboarding_questions` (startup + scheduler) with `actor=startup` or `actor=scheduler`. | Requires `ONBOARDING_TAB` and FeatureToggles enabling onboarding modules. |
| **Welcome inactivity reminders** | `modules.onboarding.watcher_welcome` | Every 15 m | Sheet-driven inactivity ladder for welcome **and promo** threads: completed sessions are skipped; CASE A (no answers) and CASE B (answers present but incomplete) both follow 3 h nudge → 24 h warning → 36 h auto-close (gated on a prior warning). Every ticket thread writes a single row to `ONBOARDING_SESSIONS` at thread creation (with `updated_at` set to the creation time); the questionnaire panel updates the same row with the panel message id. The watcher ensures the row exists before deciding reminders and only relies on answers/completed flags, reminder timestamps, and ticket age when laddering. Welcome closes rename to `Closed-W####-user-NONE` and ping recruiters to remove the user; promo closes use the same cadence without a removal ask. | `c1c.onboarding.welcome_watcher` info/WARN lines for sends, rename/archive failures, and skipped targets; warning/auto-close breadcrumbs also post to `ONBOARDING_LOG_CHANNEL_ID` when set. | `WELCOME_CHANNEL_ID`, FeatureToggles `welcome_dialog` and `recruitment_welcome`; promo flow also requires `PROMO_CHANNEL_ID` plus FeatureToggles `promo_enabled` and `enable_promo_hook`; optional `ONBOARDING_LOG_CHANNEL_ID` for Discord breadcrumbs. |
| **Cleanup watcher** | `modules.housekeeping.cleanup` | Every `CLEANUP_INTERVAL_HOURS` hours | Deletes all non-pinned messages in configured panel threads so each run resets the canvas. | Summary `🧹 Cleanup — threads=<N> • messages_deleted=<M> • errors=<E>` posted to the ops log channel plus WARN lines when fetch/delete fails. | `CLEANUP_INTERVAL_HOURS`, `CLEANUP_THREAD_IDS`. |
| **Thread keepalive** | `modules.housekeeping.keepalive` | Daily scan (acts when idle ≥ `KEEPALIVE_INTERVAL_HOURS`) | Unarchives stale target threads and posts a heartbeat to prevent auto-archive. | Summary `💙 Housekeeping: keepalive — threads_touched=<N> • errors=<E>` with WARN lines for fetch/permission failures. | `KEEPALIVE_CHANNEL_IDS`, `KEEPALIVE_THREAD_IDS`, `KEEPALIVE_INTERVAL_HOURS`. |
| **Daily Recruiter Update** | `modules.recruitment.reporting.daily_recruiter_update.scheduler_daily_recruiter_update` | Once per day at `REPORT_DAILY_POST_TIME` (UTC) | Posts the recruiter digest embed summarizing placements, queues, and cache freshness into `REPORT_RECRUITERS_DEST_ID`. | Structured console logs plus the Discord embed; scheduler start/stop events log via `daily_recruiter_update` helpers. | `REPORT_DAILY_POST_TIME`, `REPORT_RECRUITERS_DEST_ID`, and the `recruitment_reports` feature toggle. |
| **Server map refresh** | `modules.ops.server_map` | Daily interval check (24 h cadence gated by `SERVER_MAP_REFRESH_DAYS`) | Generates the category/channel overview in `#server-map`, edits existing pinned messages, and pins the first block. | Start logs note `channel_fallback` vs `requested_channel`, followed by config, optional `cleaned_messages`, and summary lines with category/channel counts plus blacklist sizes; `❌` errors still surface configuration issues. | FeatureToggles entry `SERVER_MAP` gates both the scheduler and `!servermap refresh`; `SERVER_MAP_CHANNEL_ID` and `SERVER_MAP_REFRESH_DAYS` remain env-driven while runtime state lives in the Recruitment Config tab. |
| **C1C Leagues — Monday reminder** | `modules.community.leagues.scheduler` | Weekly on Monday at `LEAGUES_REMINDER_MONDAY_UTC` (UTC) | Posts the “update the C1C_Leagues sheet” reminder into `LEAGUES_REMINDER_THREAD_ID` with admin mentions. | Reminder message in the configured thread; errors log as WARN in `c1c.community.leagues.scheduler`. | `LEAGUES_REMINDER_THREAD_ID`, `LEAGUE_ADMIN_IDS`, `LEAGUES_REMINDER_MONDAY_UTC`. |
| **C1C Leagues — Wednesday reminder** | `modules.community.leagues.scheduler` | Weekly on Wednesday at `LEAGUES_REMINDER_WEDNESDAY_UTC` (UTC) | Posts the 👍-react reminder and stores the message ID for the posting trigger; auto-reacts with 👍 for convenience. | Reminder message plus auto-reaction; reaction handling logs under `c1c.community.leagues`. | `LEAGUES_REMINDER_THREAD_ID`, `LEAGUE_ADMIN_IDS`, `LEAGUES_REMINDER_WEDNESDAY_UTC`. |

### Cleanup watcher
- **Environment.** `CLEANUP_INTERVAL_HOURS` defines the fixed interval between runs; `CLEANUP_THREAD_IDS` lists the Discord thread IDs that will be wiped.
- **Behavior.** On every run the watcher fetches the full history for each configured thread and deletes every non-pinned message, respecting Discord’s 14-day bulk delete rule (older messages fall back to one-by-one deletions). Pinned messages remain untouched.
- **Logging.** Each run emits a single summary line: `🧹 Cleanup — threads=<N> • messages_deleted=<M> • errors=<E>`. WARN lines accompany fetch, permission, or delete issues and increment the error count without blocking future runs.

### Thread keepalive
- **Environment.** `KEEPALIVE_CHANNEL_IDS` enumerates channels whose threads should be kept alive; `KEEPALIVE_THREAD_IDS` adds specific threads to the target set; `KEEPALIVE_INTERVAL_HOURS` defines the maximum idle age before a heartbeat is posted.
- **Behavior.** The job enumerates active and archived threads in configured channels, adds any explicit thread IDs, deduplicates, and skips targets missing read/send/manage-thread permissions. Threads newer than the idle threshold are ignored. Stale threads are unarchived (if needed) before posting the heartbeat message `🔹 Thread 💙-beat (housekeeping)`.
- **Logging.** Each run emits `💙 Housekeeping: keepalive — threads_touched=<N> • errors=<E>`, counting only threads that received a heartbeat. WARN lines capture fetch, permission, or send failures.

## Keepalive behaviour
The housekeeping keepalive job (above) keeps priority threads from auto-archiving.
Render also tears down idle services unless they see periodic traffic, so the
runtime keeps the bot “warm” in two additional layers:

1. **HTTP keepalive task.** `modules.common.keepalive.ensure_started()` launches a
   background task that `GET`s the configured keepalive route.
   - **Route.** `GET /keepalive` handled by the aiohttp server. Override the path
     with `KEEPALIVE_PATH`; defaults to `/keepalive`.
   - **URL resolution order.** `KEEPALIVE_URL` → `RENDER_EXTERNAL_URL` +
     `KEEPALIVE_PATH` → `http://127.0.0.1:{PORT}/keepalive` for local dev.
   - **Interval.** `KEEPALIVE_INTERVAL` seconds (minimum 60, default 300). The
     deprecated `KEEPALIVE_INTERVAL_SEC` env overrides the watchdog cadence and
     logs a warning via `config/runtime.py` for backward compatibility.
   - **Logs.** Expect `keepalive:task_started` once and recurring
     `keepalive:ping_ok` (or `keepalive:ping_fail`) lines in the bot logs.
2. **Watchdog timers.** The Discord watchdog runs on `WATCHDOG_CHECK_SEC` (360 s
   prod / 60 s non-prod) and trips after `WATCHDOG_STALL_SEC` (`check*3+30`).
   `WATCHDOG_DISCONNECT_GRACE_SEC` covers the gateway reconnect window. These
   timers exit the process when heartbeats stall so Render can restart it.

## Operations
- **Verify watcher health.**
  - Run `!ops health` or `!ops digest` to inspect cache timestamps, next refresh
    times, and watcher toggle states (mirrors `/health`).
  - Inspect `[watcher|lifecycle] …` startup logs in `LOG_CHANNEL_ID` after each
    deploy; missing lifecycle lines mean a watcher failed to register.
  - For promo/welcome incidents, disable the relevant FeatureToggles entry,
    restart via `!ops reload --reboot`, then document the change in this file.
- **Diagnose scheduler issues.** Look for `[cache] bucket=… result=error` logs or
  WARN-level entries in Render. Manual `!ops refresh <bucket>` invokes the same
  warmers and records `actor=manual` for audits.
- **Keepalive triage.** Absence of `keepalive:` logs means the ready hook never
  called `ensure_started()`; confirm `Runtime.start()` reached completion.
  Non-200 responses usually mean `KEEPALIVE_URL` points to the wrong host.
- **Daily Recruiter Update.** Use `!ops health` to check the `recruitment_reports`
  flag and verify that `scheduler_daily_recruiter_update` is running. The
  scheduler can be restarted via `modules.recruitment.reporting.daily_recruiter_update.ensure_scheduler_started()`.

## Related docs
- [`docs/Architecture.md`](../Architecture.md) — runtime surfaces and scheduler
  relationships.
- [`docs/Runbook.md`](../Runbook.md) — operational procedures that call into
  these watchers and schedulers.
- [`docs/ops/Config.md`](Config.md) — environment keys, FeatureToggles, and sheet
  tabs referenced above.
- [`docs/modules/CoreOps.md`](../modules/CoreOps.md) — runtime lifecycle,
  scheduler wiring, and watchdog contracts.
- [`docs/modules/`](../modules) — module owners for the watchers listed here.

Doc last updated: 2025-12-31 (v0.9.8.2)
