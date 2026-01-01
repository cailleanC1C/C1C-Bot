# The Woadkeeper – Full Technical Inventory (Audit Only)

## 4.1. High-Level Overview
- **Identity:** The Woadkeeper is the unified Discord bot (welcome + onboarding + recruitment + placement + ops) launched via `app.py`, which wires CoreOps helpers, loads feature-flagged modules through `Runtime.load_extensions()`, and enforces guild allow-lists and onboarding auto-capture hooks before delegating to cog commands.
- **Entrypoints:** `app.py` builds the bot, applies mention/bang command routing, captures onboarding answers in threads, starts watchdog/keepalive/scheduler loops, and triggers extension loading through `modules/common/runtime.Runtime`.
- **Domains Covered:**
  - Onboarding & welcome ticket handling (Ticket Tool threads, dialog panels, reminders, summary logging).
  - Promo/move/leadership ticket handling (R/M/L flows with clan selection and log writes).
  - Recruitment tools (clan profiles, clan search panels, recruiter panels, welcome templates, recruiter daily report).
  - Placement & reservations (reservation commands, reminders, auto-release, availability recomputation).
  - CoreOps/admin ops (health/config/env/checksheet/digest/refresh/reload/help/ping surfaces; watchdog and cache preload scheduling; server map and role map admin utilities; permissions UI).
  - Community features (Shard & Mercy tracker panel).
- **Out of Scope:** No other bots present; `modules/placement/target_select.py` and `modules/onboarding/ops_check.py` are explicitly stubs with no runtime behavior or commands.

## 4.2. Cog / Module Inventory
| Cog / Module | File Path | Purpose | User Group(s) | Sheets? | ENV? | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| CoreOpsCog | packages/c1c-coreops/src/c1c_coreops/cog.py | CoreOps surface: ops/admin health, config/env/digest/checksheet, refresh/reload, help/ping routing, command metadata wiring. | Admin/Staff | Uses cache telemetry & Sheet snapshots; relies on FeatureToggles via shared config. | Yes – BOT_NAME/BOT_VERSION, LOG_LEVEL, GUILD_IDS, log/watchdog keys, etc. | Loaded first; bang and ops prefixes. |
| Reload Cog | packages/c1c-coreops/src/c1c_coreops/commands/reload.py | Legacy reload command with onboarding reload flag and optional reboot hooks. | Admin | Sheet schemas via onboarding cache reload. | Yes – uses env reload. | Always loaded. |
| AppAdmin | cogs/app_admin.py | Admin utilities: hidden `ping`, `servermap refresh`, `whoweare` role map renderer. | Admin | Reads recruitment sheet tabs for role map. | Yes – server map/who-we-are channel IDs, feature toggles. | Loaded in runtime. |
| RecruitmentMember | cogs/recruitment_member.py | Public clan search panel launcher (`!clansearch`). | Members | Recruitment sheet cached data. | Yes – none direct, but sheet IDs for recruitment. | Panel legacy controller. |
| ClanProfileCog | cogs/recruitment_clan_profile.py | Public clan profile cards with crest toggle (`!clan`) and reaction flip listeners. | Members | Recruitment sheet clan rows & crest assets. | Yes – sheet IDs; emoji sizing env. | Reaction listeners for flip/delete cleanup. |
| RecruiterPanelCog | cogs/recruitment_recruiter.py | Recruiter clan-matching panel (`!clanmatch`) with thread routing, pager/result hydration. | Staff/Recruiters/Admin | Recruitment sheet caches. | Yes – PANEL_THREAD_MODE/ID, recruiter role IDs. | Handles fixed-thread option. |
| WelcomeBridge | cogs/recruitment_welcome.py | Staff welcome message poster (`!welcome`) and template refresher (`!welcome-refresh`). | Staff/Admin | WelcomeTemplates cache from recruitment sheet. | Yes – feature toggles gate behavior. | Mirrors legacy welcome UX. |
| RecruitmentReporting | cogs/recruitment_reporting.py | Admin command to post Daily Recruiter Update (`!report recruiters`). | Admin | Recruitment sheet report tabs. | Yes – report channel env, feature toggles. | Feature-gated scheduler companion. |
| Onboarding package preload | modules/onboarding/__init__.py | Preloads onboarding schema cache on startup. | Internal | Onboarding sheet tabs. | Yes – onboarding sheet ID/config tab. | No commands. |
| WelcomeWatcher & WelcomeTicketWatcher | modules/onboarding/watcher_welcome.py | Ticket-tool welcome watcher: detects threads, posts onboarding panels, captures answers, reminders, auto-close, summary logging, placement reconciliation. | Staff/Admin/Internal | Onboarding sheet (questions, sessions, welcome tab), recruitment & reservations sheets. | Yes – welcome/promo channel IDs, Ticket Tool ID, role IDs. | Registers reminder loop. |
| PromoTicketWatcher | modules/onboarding/watcher_promo.py | Promo/move/leadership ticket watcher: detects promo ticket threads/content markers, posts onboarding panel, collects answers, writes promo log row, handles clan selection and closure. | Staff/Admin/Internal | Onboarding sheet promo tab. | Yes – PROMO_CHANNEL_ID, Ticket Tool ID. | Uses feature toggles `promo_enabled`/`enable_promo_hook`. |
| ReactionFallback | modules/onboarding/reaction_fallback.py | Fallback 🧭 reaction handler to start onboarding panel in welcome/promo threads. | Staff/Admin/Internal | Onboarding sheet. | Yes – ticket channel IDs, role IDs. | No commands; listener only. |
| Onboarding Resume | modules/onboarding/cmd_resume.py | Recruiter recovery command `!onb resume` to restore onboarding wizard in thread. | Staff (Manage Threads) | Onboarding session cache. | Yes – none direct. | Loaded always. |
| Onboarding Ops Check | modules/onboarding/ops_check.py | Stub cog to satisfy loader (no commands). | Internal | None | Yes – none. | Stub. |
| Permissions UI | modules/ops/permissions_ui.py | Admin `!perm` interactive permissions UI with role/target/permission picker and overwrite apply. | Admin | No Sheets. | Yes – blacklist env, guild/channel IDs. | Includes preview + confirmation and per-channel logging. |
| CoreOps cmd_cfg extension | modules/coreops/cmd_cfg.py | Always-on extension exposing Ops command metadata/config helper. | Internal/Admin | Shared config snapshot. | Yes – core env keys. | Always loaded. |
| Placement Reservations | modules/placement/reservations.py | Staff reservation management (`!reserve`, `!reservations`) plus helper flows for ticket context and availability recompute. | Staff/Admin/Clan Leads (read) | Reservations sheet; recruitment sheet for clan rows. | Yes – recruiter roles/channel/thread IDs, interact channel ID. | Feature-gated by reservations toggles. |
| Placement Reservation Jobs | modules/placement/reservation_jobs.py | Scheduled reminders and auto-release for reservations, posting recruiter pings and recomputing availability. | Internal/Admin/Recruiters (notifications) | Reservations sheet | Yes – recruiter channel/thread IDs, logging channel ID. | Scheduled at 12:00Z/18:00Z via runtime scheduler. |
| Placement Target Select | modules/placement/target_select.py | Stub: logs load only. | Internal | None | Yes – none. | Feature module placeholder. |
| Recruitment Reports Loader | modules/recruitment/reports.py | Loads recruitment reporting cog when feature enabled. | Internal/Admin | Recruitment sheet | Yes – toggles. | Wrapper only. |
| Recruitment Search Loader | modules/recruitment/services/search.py | Recruiter/lead check helpers; ensures recruitment module load (no commands yet). | Internal | Recruitment sheet | Yes – none. | TODO stub. |
| ShardTracker | modules/community/shard_tracker/cog.py | Community shard & mercy tracker (`!shards`, `!shards set`) with thread panels and buttons. | Members | Milestones sheet tab for shard data. | Yes – shard mercy channel ID. | Loaded via COMMUNITY_EXTENSIONS. |
| ShardTracker Loader | modules/community/shard_tracker/__init__.py | Registers ShardTracker cog. | Internal | Milestones sheet | Yes – channel ID env. | Community extension. |
| Server Map | modules/ops/server_map.py (imported in runtime) | Scheduled/command-driven server-map embed generation. | Admin | None | Yes – SERVER_MAP_* env. | Commands live in AppAdmin `servermap refresh`. |
| Cleanup Watcher | modules/ops/cleanup_watcher.py (scheduled via runtime) | Periodic cleanup of stale threads/messages based on CLEANUP_AGE_HOURS. | Internal/Admin | None | Yes – cleanup env. | Scheduled in runtime.start. |
| Common Keepalive | modules/common/keepalive.py | HTTP keepalive endpoint registration and scheduler hooks. | Internal | None | Yes – KEEPALIVE_INTERVAL_SEC. | Always on. |
| Community Extensions Registry | modules/community/__init__.py | Lists community extensions to auto-load. | Internal | N/A | N/A | Currently shards only. |

## 4.3. Command Inventory (All Commands)
Grouped by cog/module; all commands are prefix-based unless noted.

### CoreOps / Ops (packages/c1c-coreops)
- `ops health` / hidden `health` — Admin only; checks bot health with embed summary. (`c1c_coreops/cog.py`)
- `ops checksheet` / hidden `checksheet` — Admin only; inspects configured sheet tabs/headers.
- `ops digest` / hidden `digest` — Staff (ops) or admin; quick system summary embed.
- `ops env` / hidden `env` — Admin; environment/config snapshot.
- `ops help` — User-tier; permission-aware help menu.
- `ops ping` — User-tier; routes to base ping command.
- `ops config` / hidden `config` — Admin; config snapshot embed.
- `reload` / `ops reload` — Admin; reload configs/modules, supports `--reboot` flag. (also legacy Cog in `commands/reload.py`)
- `refresh` / `ops refresh` — Admin group; refresh cache buckets (`!refresh all` supported) or root help.

### AppAdmin
- `ping` — Hidden admin health reaction. (`cogs/app_admin.py`)
- `servermap refresh` — Admin; rebuild server map embed if feature enabled.
- `whoweare` — Admin; posts Who We Are roster to configured channel from sheet.

### Recruitment Member/Staff
- `clansearch` — User; opens member clan search panel. (`cogs/recruitment_member.py`)
- `clan` — User; renders clan profile card with crest toggle reaction. (`cogs/recruitment_clan_profile.py`)
- `clanmatch` — Staff/Recruiter/Admin; opens recruiter panel, supports fixed thread routing. (`cogs/recruitment_recruiter.py`)
- `welcome` — Staff/Admin; posts templated welcome message to target channel/thread with optional note. (`cogs/recruitment_welcome.py`)
- `welcome-refresh` — Admin; reloads welcome templates cache. (`cogs/recruitment_welcome.py`)
- `report recruiters` — Admin; posts Daily Recruiter Update. (`cogs/recruitment_reporting.py`)

### Onboarding / Promo Support
- `onb resume @member` — Staff with Manage Threads; restores onboarding wizard in ticket thread. (`modules/onboarding/cmd_resume.py`)

### Placement / Reservations
- `reserve <clan> [@recruit]` — Staff/Admin; reserve seat for recruit in ticket thread; supports `release` and `extend` sub-commands. (`modules/placement/reservations.py`)
- `reservations [clan_tag]` — Staff/Admin; shows active reservations for recruit or clan (clan scope restricted to interact channel; clan leads can view). (`modules/placement/reservations.py`)

### Permissions
- `perm` — Admin; launch the interactive Permissions UI and apply role overwrites. (`modules/ops/permissions_ui.py`)

### Community / Shards
- `shards [type]` — User; opens shard tracker panel in dedicated channel/thread. (`modules/community/shard_tracker/cog.py`)
- `shards set <type> <count>` — User; set stash count for shard type. (`modules/community/shard_tracker/cog.py`)

### Diagnostic / Hidden Behaviors
- Base mention commands: `@Bot help` and `@Bot ping` proxy to CoreOps equivalents via `app.py` mention routing.
- Hidden admin ping reaction via AppAdmin; refresh/reload roots provide usage hints when invoked without args.

## 4.4. Feature Areas (by Domain)
### Onboarding & Welcome Tickets
- **Modules/Cogs:** `modules/onboarding/watcher_welcome.py`, `modules/onboarding/reaction_fallback.py`, `modules/onboarding/cmd_resume.py`, onboarding preload (`modules/onboarding/__init__.py`).
- **Commands/Interactions:** No public commands except `!onb resume`; panels auto-start via Ticket Tool messages or 🧭 reaction fallback; answers captured from thread messages in `app.py`.
- **Config:** Onboarding sheet (questions tab via `ONBOARDING_TAB`, sessions tab, welcome tickets tab), FeatureToggles `WELCOME_ENABLED`/`ENABLE_WELCOME_HOOK`/`welcome_dialog`, env `WELCOME_CHANNEL_ID`, `TICKET_TOOL_BOT_ID`, role IDs.

### Promo / Move / Leadership Tickets
- **Modules:** `modules/onboarding/watcher_promo.py`.
- **Triggers:** Promo ticket threads in promo channel or content markers; feature toggles `PROMO_ENABLED`/`ENABLE_PROMO_HOOK`; optional clan select UI.
- **Config:** Promo tab (`PROMO_TICKETS_TAB`), promo channel env, Ticket Tool ID, FeatureToggles.

### Recruitment & Templates
- **Modules/Cogs:** `cogs/recruitment_welcome.py`, `cogs/recruitment_recruiter.py`, `cogs/recruitment_member.py`, `cogs/recruitment_clan_profile.py`, recruitment sheet caches/services, `modules/recruitment/reports.py` + `cogs/recruitment_reporting.py`.
- **Commands:** `welcome`, `welcome-refresh`, `clanmatch`, `clansearch`, `clan`, `report recruiters`.
- **Config:** Recruitment sheet ID/config tab; FeatureToggles (`recruitment_welcome`, `recruiter_panel`, `member_panel`, `recruitment_reports`); env channel IDs for recruiters/logging; PANEL_THREAD_MODE/ID.

### Placement & Reservations
- **Modules:** `modules/placement/reservations.py`, `modules/placement/reservation_jobs.py`, stub `modules/placement/target_select.py`.
- **Commands:** `reserve`, `reservations`.
- **Config:** Reservations sheet (ledger), recruitment sheet for clan rows, interact/recruiter channel IDs, clan lead IDs, FeatureToggles `feature_reservations`/`placement_reservations`.

### Shard Tracking
- **Modules:** `modules/community/shard_tracker/*`.
- **Commands:** `shards`, `shards set`.
- **Config:** Milestones sheet (`SHARD_MERCY_TAB`), FeatureToggles/Channel ID for shard tracking.

### CoreOps / Admin Ops
- **Modules:** `packages/c1c-coreops`, `modules/coreops/cmd_cfg.py`, runtime scheduler/keepalive.
- **Commands:** health, env, config, digest, checksheet, refresh, reload, help, ping; admin bang aliases.
- **Config:** Broad ENV set (tokens, guild IDs, watchog timings, log channel IDs), FeatureToggles for ops modules, cache schedules from shared config.

### Ops & Infra Helpers
- **Modules:** `modules/ops/permissions_ui.py`, `modules/ops/server_map.py`, `modules/ops/cleanup_watcher.py`, `modules/common/keepalive.py`.
- **Commands:** `perm`, `servermap refresh`; permissions UI and server map tooling; hidden ping.
- **Config:** `PERMS_BLACKLIST_*`, `SERVER_MAP_*` env, `CLEANUP_AGE_HOURS`, permission role IDs.

### Community-Facing Systems
- **Shard tracker** (above) and **Who We Are** roster via `whoweare` command; server map embeds; clan profile/search panels.

## 4.5. Configuration Map (Expanded)
### ENV Variables (grouped)
- **Core runtime:** `DISCORD_TOKEN`, `ENV_NAME`, `BOT_NAME`, `BOT_VERSION`, `GUILD_IDS`, `TIMEZONE`, `LOG_LEVEL`, `LOG_CHANNEL_ID`, `PUBLIC_BASE_URL`, `RENDER_EXTERNAL_URL`, `WATCHDOG_CHECK_SEC`, `WATCHDOG_STALL_SEC`, `WATCHDOG_DISCONNECT_GRACE_SEC`, `KEEPALIVE_INTERVAL_SEC`, `PORT`.
- **Sheets access:** `GSPREAD_CREDENTIALS`/`GOOGLE_SERVICE_ACCOUNT_JSON`, `RECRUITMENT_SHEET_ID`, `ONBOARDING_SHEET_ID`, `REMINDER_SHEET_ID`, `MILESTONES_SHEET_ID`, config tab overrides (`RECRUITMENT_CONFIG_TAB`, `ONBOARDING_CONFIG_TAB`, `WORKSHEET_NAME`), sheets cache TTL settings.
- **Roles/Channels:** `ADMIN_ROLE_IDS`, `STAFF_ROLE_IDS`, `RECRUITER_ROLE_IDS`, `LEAD_ROLE_IDS`, `CLAN_LEAD_IDS`, `ADMIN_IDS`, `RECRUITERS_CHANNEL_ID`, `RECRUITERS_THREAD_ID`, `RECRUITMENT_INTERACT_CHANNEL`, `WELCOME_GENERAL_CHANNEL_ID`, `WELCOME_CHANNEL_ID`, `PROMO_CHANNEL_ID`, `NOTIFY_CHANNEL_ID`, `NOTIFY_PING_ROLE_ID`, `LOGGING_CHANNEL_ID`, `SERVER_MAP_CHANNEL_ID`, `SERVER_MAP_CATEGORY_BLACKLIST`, `SERVER_MAP_CHANNEL_BLACKLIST`, `WHO_WE_ARE_CHANNEL_ID`, `PANEL_THREAD_MODE`, `PANEL_FIXED_THREAD_ID`, `REPORT_RECRUITERS_DEST_ID`, `TICKET_TOOL_BOT_ID`, `SHARD_MERCY_CHANNEL_ID`.
- **Runtime flags:** `STRICT_PROBE`, `SEARCH_RESULTS_SOFT_CAP`, `STRICT_EMOJI_PROXY`, emoji sizing envs (`EMOJI_MAX_BYTES`, `EMOJI_PAD_SIZE`, etc.), cache TTLs (`CLAN_TAGS_CACHE_TTL_SEC`, `SHEETS_CACHE_TTL_SEC`), cleanup (`CLEANUP_AGE_HOURS`), watchdog/refresh timings, `REFRESH_TIMES`.
- **Placement/Reservations:** `RECRUITMENT_INTERACT_CHANNEL`, `RECRUITERS_CHANNEL_ID`, `RECRUITERS_THREAD_ID`, `CLAN_LEAD_IDS`, `REPORT_RECRUITERS_DEST_ID`, `LOGGING_CHANNEL_ID` (auto-release summaries).
- **Server Map / Who We Are:** `SERVER_MAP_*`, `WHO_WE_ARE_CHANNEL_ID`.
- **Shard Tracker:** `SHARD_MERCY_CHANNEL_ID` from milestones config tab.

### Sheets
- **Recruitment Sheet:** `FeatureToggles` tab (feature flags), clan roster/profile data, role map tab (cluster role map), Daily Recruiter Update tabs, WelcomeTemplates cache source.
- **Onboarding Sheet:** `ONBOARDING_TAB` (questions), `ONBOARDING_SESSIONS_TAB` (session state), `WELCOME_TICKETS_TAB` (welcome log), `PROMO_TICKETS_TAB` (promo/move/leadership log), `CLANLIST_TAB` (clan tags for validation), config tab for channels/IDs.
- **Reservations Sheet:** Reservation ledger tab (active reservations, expiry dates, thread IDs, user snapshots).
- **Milestones Sheet:** `SHARD_MERCY_TAB` for shard tracker records and config (emojis/tab mappings).
- **Reminders/Milestones/Other:** Reminder sheet (reminder jobs), telemetry uses cache buckets for clans/templates/onboarding_questions, etc.

### Local Config Files
- `AUDIT/diagnostics/*` — optional diagnostic outputs (not runtime inputs).

## 4.6. Background Jobs, Watchers, Schedulers
- **Runtime watchdog/keepalive:** Started in `app.py` and `modules/common/runtime` (heartbeat, socket touch, keepalive web route); watchdog logs interval/stall/grace settings.
- **Cache preload & refresh scheduler:** `modules/common/runtime.start` registers periodic refresh jobs for cache buckets `clans`, `templates`, `clan_tags`, `onboarding_questions` with specified cadences (3h/7d/7d/7d) and emits schedule log; startup preload refreshes all buckets once.
- **Cleanup watcher:** Scheduled via runtime using `modules/ops/cleanup_watcher` every `CLEANUP_AGE_HOURS` to prune stale items.
- **Server map job:** `modules/ops/server_map.schedule_server_map_job` invoked during runtime start when feature enabled to refresh map posts on cadence from env.
- **Daily summary cron:** In `app.py` runtime.on_ready`, spawns daily summary emitter at 00:05Z (`emit_daily_summary`) plus Daily Recruiter Update scheduler bootstrap (`ensure_scheduler_started`).
- **Welcome reminders:** `_ensure_reminder_job` in `modules/onboarding/watcher_welcome.py` schedules 15-minute scans for incomplete sessions with staged reminders (3h/5h first reminders, 24h warning, 36h auto-close) respecting answered state.
- **Welcome watcher listeners:** Detect Ticket Tool events/close messages, handle button triggers, auto-panel start, message capture for answers, finalize logging/reservation reconciliation, thread rename on close.
- **Promo watcher listeners:** `modules/onboarding/watcher_promo.py` monitors promo channel threads, Ticket Tool messages with markers, handles panel posting, clan select view, writes promo rows, renames threads and logs on closure.
- **Reaction fallback listener:** `modules/onboarding/reaction_fallback.py` handles 🧭 reaction by staff/admin/guardian knights to launch onboarding panel in welcome/promo threads.
- **Reservation jobs:** `modules/placement/reservation_jobs.py` schedules daily 12:00Z reminders to recruiters for expiring reservations and 18:00Z auto-release of overdue reservations, posting summaries and recomputing availability.
- **Shard tracker UI:** `modules/community/shard_tracker/cog.py` manages persistent views per-thread; no scheduled jobs, but relies on channel constraint.
- **CoreOps help/ping routing:** `app.py` mention/bang handlers intercept messages to route help/ping through ops commands; onboarding auto-capture from thread messages runs on every message.

## 4.7. Legacy / Ambiguous / Possibly Unused
- `modules/placement/target_select.py` — explicit stub; logs load only (no commands/behavior).
- `modules/onboarding/ops_check.py` — stub cog for loader stability.
- `modules/recruitment/services/search.py` — TODO stub; no commands beyond ensure_loaded hook.
- `modules/coreops/cmd_cfg.py` — always-on helper extension; internal only.
- `packages/c1c-coreops/commands/reload.py` — legacy reload command coexisting with CoreOps reload; both loaded, but CoreOps `reload` commands already provided.
- Older audit files under AUDIT/ are excluded by guardrail; not used at runtime.
