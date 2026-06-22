# Configuration Reference

This page is the single source of truth for runtime configuration. Update it alongside
[`.env.example`](.env.example) so the template stays in parity with production settings.

## Live `!config` embed example
```
Configuration Snapshot — prod
Guilds: Clash Champs (Recruitment), Clash Champs Lounge (Onboarding)
Sheets: Recruitment → 1aBCDefGhijKLMnoPqrStuV · Onboarding → 9zYXwvUTsrQpoNMlkJihGFed
Watchers: welcome✅ promo✅
Toggles: STRICT_PROBE=off · SEARCH_RESULTS_SOFT_CAP=25
Meta: Cache age 42s · Next refresh 02:15 UTC · Actor startup
```

- Guild display names replace raw snowflake IDs across the embed.
- Recruitment and Onboarding Sheet IDs appear in full; click-through URLs remain hidden to avoid clutter.
- The meta overlay surfaces cache age, next refresh, and actor pulled from the public telemetry snapshot.
- Date/time fields are removed entirely. Embed footers continue to show `Bot vX.Y.Z · CoreOps vA.B.C` with no timestamp block.

## `!env` overview

- The `!env` command now renders four pages in one reply (Overview, Channels, Roles, Sheets & Config) using the admin colour.
- Page 1 surfaces Feature Toggles and a Warnings block when values are missing or Discord lookups fail (max 10 lines).
- When warnings exist, a single `⚠ env:missing_config` log line captures the affected keys for quick investigation.

## Environment keys

> The keys below are authoritative. `.env.example` mirrors this list; CI enforces parity.

**Required at startup:** `DISCORD_TOKEN`, `GSPREAD_CREDENTIALS`, `RECRUITMENT_SHEET_ID`

**Optional for startup:** `ONBOARDING_SHEET_ID`*, `ENV_NAME`, `BOT_NAME`, `PUBLIC_BASE_URL`, `RENDER_EXTERNAL_URL`, `LOG_CHANNEL_ID`, `LOGGING_CHANNEL_ID`, `RECRUITERS_CHANNEL_ID`, `SERVER_MAP_CHANNEL_ID`, `SERVER_MAP_CATEGORY_BLACKLIST`, `SERVER_MAP_CHANNEL_BLACKLIST`, `WHO_WE_ARE_CHANNEL_ID`, `SERVER_MAP_REFRESH_DAYS`, `WATCHDOG_CHECK_SEC`, `WATCHDOG_STALL_SEC`, `WATCHDOG_DISCONNECT_GRACE_SEC`

All sheet-facing modules now require their dedicated `*_SHEET_ID` variables; 

Missing any **Required** key causes the bot to exit with an error at startup. If `LOG_CHANNEL_ID` is empty, Discord channel logging is disabled and a one-time startup warning is emitted.

\* Leaving `ONBOARDING_SHEET_ID` empty allows the process to boot, but onboarding watchers, cache refreshes, and questionnaire-driven commands either skip their work or emit soft errors.

### Core runtime
| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `DISCORD_TOKEN` | secret | — | Bot token for the Discord application (masked in logs). |
| `ENV_NAME` | string | `dev` | Environment label (influences watchdog defaults). |
| `BOT_NAME` | string | `C1C-Recruitment` | Display name surfaced in telemetry. |
| `BOT_VERSION` | string | `dev` | Version string surfaced in embeds and logs. |
| `GUILD_IDS` | csv | — | Comma-separated guild IDs allowed to load the bot. |
| `TIMEZONE` | string | `Europe/Vienna` | Olson timezone used for embeds and scheduling. |
| `REFRESH_TIMES` | csv | `02:00,10:00,18:00` | Optional daily refresh windows (HH:MM, comma separated). |
| `PORT` | int | `10000` | Render injects this automatically; local runs fall back to 10000. |
| `LOG_LEVEL` | string | 'INFO' | Python logging level. |
| `LOG_CHANNEL_ID` | snowflake | — | Required for Discord channel logging. If unset or empty, logging to Discord is disabled and a one-time startup warning is emitted. No implicit defaults. |

### Google Sheets access
| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `GSPREAD_CREDENTIALS` | secret | — | Primary service account JSON used for Sheets access (preferred over legacy alias). |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | secret | — | Legacy alias for `GSPREAD_CREDENTIALS`. |
| `RECRUITMENT_SHEET_ID` | string | — | Google Sheet ID for recruitment data. |
| `ONBOARDING_SHEET_ID` | string | — | Google Sheet ID for onboarding trackers. |
| `REMINDER_SHEET_ID` | string | — | Google Sheet ID for reminders (service-specific). |
| `MILESTONES_SHEET_ID` | string | — | Google Sheet ID for Milestones (claims, appreciation, shard & mercy, missions) (service-specific). |
| `RECRUITMENT_CONFIG_TAB` | string | `Config` | Worksheet name containing recruitment config. |
| `ONBOARDING_CONFIG_TAB` | string | `Config` | Worksheet name containing onboarding config. |
| `WORKSHEET_NAME` | string | `bot_info` | Fallback for the `clans_tab` worksheet when sheet config is missing. |
| `GSHEETS_RETRY_ATTEMPTS` | int | `5` | Default retry attempts for Sheets API requests. |
| `GSHEETS_RETRY_BASE` | float | `0.5` | Base delay (seconds) for Sheets exponential backoff. |
| `GSHEETS_RETRY_FACTOR` | float | `2.0` | Multiplier for Sheets exponential backoff. |
| `SHEETS_CACHE_TTL_SEC` | int | `900` | TTL for cached worksheet values. |
| `SHEETS_CONFIG_CACHE_TTL_SEC` | int | matches `SHEETS_CACHE_TTL_SEC` | TTL for cached worksheet metadata; defaults to the value above. |
| `SHEETS_EXPORT_DELAY_MS` | int | `0` | Optional throttle (milliseconds) applied after each Google Sheets/Drive export (PDF/PNG). |

Async handlers must import Sheets helpers from `shared.sheets.async_facade`; the
sync modules remain available for non-async scripts and cache warmers.

### Role and channel routing
| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `ADMIN_ROLE_IDS` | csv | — | Elevated admin role IDs. |
| `STAFF_ROLE_IDS` | csv | — | Staff role IDs (welcome + refresh tools). |
| `RECRUITER_ROLE_IDS` | csv | — | Recruiter role IDs (panels, digests). |
| `LEAD_ROLE_IDS` | csv | — | Lead role IDs for escalations. |
| `CLAN_LEAD_IDS` | csv | — | Discord user IDs allowed to inspect clan-level reservations in the interact channel. |
| `ADMIN_IDS` | csv | — | Optional list of Discord user IDs treated as admins. |
| `RECRUITERS_CHANNEL_ID` | snowflake | — | Recruiter lounge channel receiving reservation reminders (with ticket jump links). |
| `RECRUITERS_THREAD_ID` | snowflake | — | Thread receiving recruitment updates. |
| `RECRUITMENT_INTERACT_CHANNEL` | snowflake | — | Channel where recruiters and clan leads review reservation rosters. |
| `WELCOME_GENERAL_CHANNEL_ID` | snowflake | — | Public welcome channel ID (optional). |
| `WELCOME_CHANNEL_ID` | snowflake | — | Private welcome ticket channel ID. |
| `TICKET_TOOL_BOT_ID` | snowflake | — | Discord user ID of the Ticket Tool bot. Required when Ticket Tool-driven ticket workflows (welcome/onboarding/reservations) are enabled so threads can be associated with the correct bot. |
| `PROMO_CHANNEL_ID` | snowflake | — | Promo ticket channel ID used for R/M/L promo tickets. |
| `NOTIFY_CHANNEL_ID` | snowflake | — | Fallback alert channel ID. |
| `NOTIFY_PING_ROLE_ID` | snowflake | — | Role pinged for urgent alerts. |
| `LOGGING_CHANNEL_ID` | snowflake | — | Logging/audit channel receiving structured reservation auto-release entries. |
| `ONBOARDING_LOG_CHANNEL_ID` | snowflake | — | Optional channel receiving onboarding inactivity warnings and auto-close breadcrumbs. |
| `SERVER_MAP_CHANNEL_ID` | snowflake | — | Discord channel hosting the server map embed when the SERVER_MAP toggle is enabled. |
| `SERVER_MAP_CATEGORY_BLACKLIST` | csv | — | Comma-separated Discord category IDs hidden from the rendered server map. |
| `SERVER_MAP_CHANNEL_BLACKLIST` | csv | — | Comma-separated Discord channel IDs hidden from the server map, even when their parent category is visible. |
| `WHO_WE_ARE_CHANNEL_ID` | snowflake | — | Discord channel ID used by the `!whoweare` role map command. |
| `PANEL_THREAD_MODE` | enum | `same` | `same` posts panels in the invoking channel; `fixed` routes to a dedicated thread. |
| `PANEL_FIXED_THREAD_ID` | snowflake | — | Thread used when `PANEL_THREAD_MODE=fixed`. |
| `RAID_ROLE_ID` | snowflake | — | Raid role granted to active clan members. |
| `WANDERING_SOULS_ROLE_ID` | snowflake | — | Role for members without clan tags. |
| `VISITOR_ROLE_ID` | snowflake | — | Visitor role used during welcome ticketing. |
| `CLAN_ROLE_IDS` | csv | — | Comma-separated list of all clan-tag role IDs. |
| `ADMIN_AUDIT_DEST_ID` | snowflake | — | Channel or thread receiving the housekeeping role/visitor audit. |
| `REPORT_RECRUITERS_DEST_ID` | snowflake | — | Channel or thread receiving the Daily Recruiter Update. |

### C1C Leagues autoposter
| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `C1C_LEAGUE_ROLE_ID` | snowflake | — | Discord role ID for **@C1CLeague**; granted automatically on first image submission and mentioned in announcements. |
| `ANNOUNCEMENT_CHANNEL_ID` | snowflake | — | Channel receiving the weekly cross-league announcement (jump links + @C1CLeague). |
| `LEAGUES_SHEET_ID` | string | — | Google Sheet ID for the `C1C_Leagues` workbook; Config tab declares ranges for each league. |
| `LEAGUES_CONFIG_TAB` | string | `Config` | Optional override for the Leagues Config tab name. |
| `LEAGUES_SUBMISSION_CHANNEL_ID` | snowflake | — | Channel watched for image uploads; grants the C1CLeague role on first qualifying attachment. |
| `LEAGUES_LEGENDARY_THREAD_ID` | snowflake | — | Thread receiving the Legendary League header + 9 board PNGs. |
| `LEAGUES_RISING_THREAD_ID` | snowflake | — | Thread receiving the Rising Stars League header + 7 board PNGs. |
| `LEAGUES_STORMFORGED_THREAD_ID` | snowflake | — | Thread receiving the Stormforged League header + 4 board PNGs. |
| `LEAGUES_REMINDER_THREAD_ID` | snowflake | — | Thread used for Monday and Wednesday reminders plus job-status/error summaries. |
| `LEAGUE_ADMIN_IDS` | csv | — | Discord user IDs allowed to confirm Wednesday reminders via 👍 and to trigger manual posts. |
| `LEAGUES_REMINDER_MONDAY_UTC` | HH:MM | `13:00` | UTC time for the Monday reminder asking admins to update the Leagues sheet. |
| `LEAGUES_REMINDER_WEDNESDAY_UTC` | HH:MM | `13:00` | UTC time for the Wednesday reminder + reaction check that triggers posting. |

### Runtime flags
| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `STRICT_PROBE` | bool | `false` | Enforces guild allow-list before startup completes. |
| `SEARCH_RESULTS_SOFT_CAP` | int | `25` | Soft limit on search results per query. |

> Feature toggles such as `recruitment_reports`, `placement_target_select`, `placement_reservations`, and onboarding watcher flags are sourced from the FeatureToggles worksheet.

### Watchdog, cache, and cleanup
| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `WATCHDOG_CHECK_SEC` | int | `360` prod / `60` non-prod | Derived from `ENV_NAME`; controls watchdog heartbeat cadence. |
| `WATCHDOG_STALL_SEC` | int | `keepalive*3+30` | Stall threshold derived from the heartbeat cadence. |
| `WATCHDOG_DISCONNECT_GRACE_SEC` | int | `WATCHDOG_STALL_SEC` | Disconnect grace period; falls back to stall threshold. |
| `KEEPALIVE_INTERVAL_SEC` | int | — | Legacy alias for `WATCHDOG_CHECK_SEC`; logs a warning when used. |
| `CLAN_TAGS_CACHE_TTL_SEC` | int | `3600` | TTL for cached clan tags. |
| `REPORT_DAILY_POST_TIME` | HH:MM | `09:30` | UTC time for the Daily Recruiter Update scheduler. |
| `SERVER_MAP_REFRESH_DAYS` | int | `30` | Minimum days between scheduled server map refreshes; enforced alongside sheet runtime state. |
| `CLEANUP_INTERVAL_HOURS` | int | `24` | Interval (hours) between cleanup sweeps; each run removes all non-pinned messages in configured threads. |
| `CLEANUP_THREAD_IDS` | csv | — | Comma-separated Discord thread IDs where cleanup wipes all non-pinned messages. |
| `HOUSEKEEPING_KEEPALIVE_ENABLED` | Feature Toggles tab bool | — | Required sheet toggle for thread keepalive; missing/blank/invalid disables scheduling without Config or ENV fallback. |
| `HOUSEKEEPING_KEEPALIVE_TAB` | Config tab string | — | Required name of the keepalive target tab; no code default or fallback tab name. |
| `HOUSEKEEPING_KEEPALIVE_DEFAULT_MESSAGE` | Config tab string | — | Required global fallback keepalive message; blank means stale rows without row/parent messages are skipped safely. |
| `HOUSEKEEPING_KEEPALIVE_STALE_AFTER_HOURS` | Config tab number | — | Required inactivity threshold before posting into a thread. |
| `HOUSEKEEPING_KEEPALIVE_RUN_EVERY_HOURS` | Config tab number | — | Required scheduler cadence for checking the keepalive sheet. |

### Media rendering
| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `PUBLIC_BASE_URL` | url | — | External base URL for `/emoji-pad`; falls back silently to `RENDER_EXTERNAL_URL` when unset. |
| `RENDER_EXTERNAL_URL` | url | — | Render.com external hostname used when `PUBLIC_BASE_URL` is not provided. |
| `EMOJI_MAX_BYTES` | int | `2000000` | Maximum emoji payload size accepted by `/emoji-pad`. |
| `EMOJI_PAD_SIZE` | int | `256` | Square canvas dimension for padded emoji PNGs. |
| `EMOJI_PAD_BOX` | float | `0.85` | Fraction of the canvas filled by the emoji glyph after padding. |
| `TAG_BADGE_PX` | int | `128` | Pixel edge length used when generating clan badge attachments. |
| `TAG_BADGE_BOX` | float | `0.90` | Glyph fill ratio applied during clan badge attachment rendering. |
| `STRICT_EMOJI_PROXY` | bool | `true` | When truthy (`1`), require padded proxy thumbnails instead of raw CDN URLs. |
| `SHARD_PANEL_OVERVIEW_EMOJI` | string | `c1c` | Emoji tag or `<:name:id>` value used for the shard overview tab author icon. |
| `SHARD_EMOJI_ANCIENT` | string | `ancient` | Emoji tag or `<:name:id>` value for the Ancient shard tab/button. |
| `SHARD_EMOJI_VOID` | string | `void` | Emoji tag or `<:name:id>` value for the Void shard tab/button. |
| `SHARD_EMOJI_SACRED` | string | `sacred` | Emoji tag or `<:name:id>` value for the Sacred shard tab/button. |
| `SHARD_EMOJI_PRIMAL` | string | `primal` | Emoji tag or `<:name:id>` value for the Primal shard tab/button. |

> Local development runner (`scripts/dev_run.sh`) now sources `.env` with `set -a; source ./.env`, preserving quoted and space-containing values verbatim.
>
> The `/emoji-pad` proxy enforces HTTPS-only source URLs. Any HTTP attempt returns `400 invalid source host`.

### Recruitment summary theme
- The welcome summary embed resolves its title icon and colour via `shared.theme` helpers. No new environment keys are required; existing `ICON_*` or `COLOR_*` overrides apply automatically.

## Automation listeners & cron jobs

Event listeners and watcher availability are controlled by FeatureToggles sheet entries rather than ENV variables.

> Cron cadences are fixed in code today; scheduled jobs refresh the `clans`, `templates`, and `clan_tags` cache buckets, post `[cache]` summaries to the ops channel, and emit the Daily Recruiter Update at `REPORT_DAILY_POST_TIME` (UTC). Update the scheduler directly if the defaults change.

## Sheet config tabs
Both Google Sheets referenced above must expose a `Config` worksheet with **Key** and **Value** columns.

### Recruitment runtime state keys
- `SERVER_MAP_MESSAGE_ID_1`, `SERVER_MAP_MESSAGE_ID_2`, … — message IDs for each segment of the server map post in `SERVER_MAP_CHANNEL_ID`. The scheduler edits these messages in place when the structure changes.
- `SERVER_MAP_LAST_RUN_AT` — ISO-8601 timestamp recorded after every successful refresh. The daily job reads this value to enforce `SERVER_MAP_REFRESH_DAYS`.

Blacklist keys for server map rendering are environment variables (`SERVER_MAP_CATEGORY_BLACKLIST`, `SERVER_MAP_CHANNEL_BLACKLIST`) rather than sheet config entries.

The `SERVER_MAP` FeatureToggle in the FeatureToggles worksheet still gates the automation. These blacklist keys only hide specific entries; they do not disable scheduling or posting.

Permissions UI blacklist keys are also environment variables:
- `PERMS_BLACKLIST_CHANNEL_IDS` — comma-separated channel IDs excluded from `!perm`.
- `PERMS_BLACKLIST_CATEGORY_IDS` — comma-separated category IDs excluded from `!perm` (and their child channels).

### Recruitment sheet keys
- 'CLANS_TAB'
- 'WELCOME_TEMPLATES_TAB'
- 'FEATURE_TOGGLES_TAB'
- 'REPORTS_TAB'
- 'RESERVATIONS_TAB'
- 'ROLEMAP_TAB'

`RESERVATIONS_TAB` defaults to `Reservations` and stores the structured reservation
ledger used to derive availability. If the key is missing, the adapter falls back
to the default name so new environments remain inert until the sheet configuration
is populated.

`ROLEMAP_TAB` defaults to `WhoWeAre` and stores the category/role listings that
power the `!whoweare` cluster role map command. The worksheet must expose the
columns `category`, `role_ID`, `role_name`, and `role_description` so the bot
can group roles, resolve Discord IDs, and surface the sheet's snarky blurbs.

### Sheet-based Feature Toggles (`Feature_Toggles` tab)

The `Feature_Toggles` worksheet in the Recruitment sheet stores **sheet-only
feature flags**. These keys are **not** environment variables and must **not** be
added to `.env.example`.

Current keys include (non-exhaustive):

- `FEATURE_ONBOARDING` — gates the onboarding flow.
- `FEATURE_REPORTS` — gates recruiter reports.
- `FEATURE_RESERVATIONS` — gates reservation-aware workflows (e.g., `!reserve`,
  reminder jobs) once implemented.
- `SERVER_MAP` — enables the scheduled refresh and manual `!servermap refresh`
  command; channel routing (`SERVER_MAP_CHANNEL_ID`) and cadence
  (`SERVER_MAP_REFRESH_DAYS`) remain environment-driven.
- `ClusterRoleMap` — enables the `!whoweare` command that renders the "Who We
  Are" roster from the configured `ROLEMAP_TAB` worksheet.

If a toggle key is missing in `Feature_Toggles`, its behaviour should default to a
safe value (usually `FALSE`/disabled) so new environments remain inert until
explicitly configured in the sheet.

### Onboarding
- `ONBOARDING_TAB` (string) — Sheet tab name containing the onboarding questions with headers `flow, order, qid, label, type, required, maxlen, validate, help, options, visibility_rules, nav_rules`. Preloaded at startup and refreshed weekly; missing or invalid values surface `missing config key: ONBOARDING_TAB` during refresh.
- `ONBOARDING_SESSIONS_TAB` (string) — Worksheet storing onboarding sessions keyed by `user_id` + `thread_id`. Columns (order enforced): `user_id`, `thread_id`, `panel_message_id`, `step_index`, `completed`, `completed_at`, `answers_json`, `updated_at`, `first_reminder_at`, `warning_sent_at`, `auto_closed_at`.
- Feature toggles `PROMO_ENABLED` and `promo_dialog` gate promo onboarding dialogs (`promo.r`, `promo.m`, `promo.l`). Both must be enabled for promo dialogs to run once detected.

### Onboarding sheet keys
- 'ONBOARDING_TAB'
- 'WELCOME_TICKETS_TAB'
- 'PROMO_TICKETS_TAB'
- `PROMO_SOURCE_CLAN_TAG_HEADER` (required; value `source_clan_tag`)
- 'CLANLIST_TAB'

**Promo tab columns (PROMO_TICKETS_TAB)**

```
ticket number | username | clantag | source_clan_tag | date closed | type | thread created | year | month | join_month | clan name | progression
```

`source_clan_tag` records the previous/came-from clan selected during promo/move close; `clantag` remains the destination/final clan used in the closed thread name. The bot resolves this source column through `PROMO_SOURCE_CLAN_TAG_HEADER`; missing Config/header mapping is treated as an operational error instead of silently falling back to destination-only math.

Promo tickets use the R/M/L prefixes to map to `type` values:

- `R####` → `returning player`
- `M####` → `player move request`
- `L####` → `clan lead move request`

Leave values blank only if a module is disabled via toggles.

#### Feature Toggles
Lists the current feature toggles loaded from the FeatureToggles sheet.

Example (sheet-sourced):

```
Feature Toggles:
  member_panel = ON
  recruiter_panel = ON
  clan_profile = ON
  recruitment_welcome = ON
  recruitment_reports = ON
  welcome_dialog = ON
  promo_dialog = ON
  promo_enabled = ON
  enable_promo_hook = ON
  placement_target_select = ON
  placement_reservations = ON
  FEATURE_RESERVATIONS = ON
  ClusterRoleMap = ON
  SERVER_MAP = ON
  WELCOME_ENABLED = ON
  ENABLE_WELCOME_HOOK = ON
  PROMO_ENABLED = ON
  ENABLE_PROMO_HOOK = ON
  housekeeping_enabled = ON
  mirralith_overview_enabled = ON
  ops_permissions_enabled = ON
  promo_watcher_enabled = ON
  resume_command_enabled = ON
  welcome_watcher_enabled = ON
```

### Feature toggle highlights

- `WELCOME_ENABLED`, `ENABLE_WELCOME_HOOK`, `recruitment_welcome`, `welcome_dialog` — welcome watcher activation and dialog control, all gated through `modules.common.feature_flags.is_enabled()`.
- `PROMO_ENABLED`, `ENABLE_PROMO_HOOK`, `promo_enabled`, `enable_promo_hook`, `promo_dialog` — promo watcher activation + dialog gating.
- `welcome_dialog`, `promo_dialog` — dialog/panel toggles; promo dialog is reserved for future onboarding steps.
- `housekeeping_enabled`, `HOUSEKEEPING_KEEPALIVE_ENABLED`, `mirralith_overview_enabled` — gate cleanup cadences, thread keepalive scheduling, and the Mirralith overview autoposter scheduler.
- `ops_permissions_enabled` — gate Permissions UI registration.
- `welcome_watcher_enabled`, `promo_watcher_enabled`, `resume_command_enabled` — gate onboarding watcher registration and `!onb resume`; these default to ON when rows are missing so existing deployments remain unchanged.

### Milestones sheet keys
- `SHARD_MERCY_TAB` — worksheet name that stores the Shard & Mercy tracker rows
  inside `MILESTONES_SHEET_ID`. No defaults; the config tab must provide the
  exact tab name.
- `SHARD_MERCY_CHANNEL_ID` — Discord channel ID dedicated to shard tracking.
  Commands run outside this channel reply with a routing reminder; the value is
  read from the same milestones Config tab so shard routing stays sheet-driven.
- `SHARD_CLANS_TAB` — worksheet name used for clan sharing + weekly shard reminders.
- `SHARD_REMINDER_TAB` — worksheet name for durable weekly reminder dedupe rows
  (`clan_key`, `window_key`, `reminder_type`, `sent_at_utc`).

### Feature toggles worksheet

**Config key**

| KEY | VALUE |
| --- | --- |
| 'FEATURE_TOGGLES_TAB' | `FeatureToggles` |

**FeatureToggles tab (recruitment Sheet)**

- Headers: `feature_name`, `enabled` (case-insensitive).
- **Only 'TRUE' (ON) enables a feature.** Any other value ('FALSE', numbers, text, blank) disables it.
- Seed rows:
  ```
  feature_name,enabled
  member_panel,TRUE
  recruiter_panel,TRUE
  clan_profile,TRUE
  recruitment_welcome,TRUE
  recruitment_reports,TRUE
  welcome_dialog,TRUE
  promo_dialog,TRUE
  promo_enabled,TRUE
  enable_promo_hook,TRUE
  placement_target_select,TRUE
  placement_reservations,TRUE
  FEATURE_RESERVATIONS,TRUE
  ClusterRoleMap,TRUE
  SERVER_MAP,TRUE
  housekeeping_enabled,TRUE
  mirralith_overview_enabled,TRUE
  c1c_ad,TRUE
  ops_permissions_enabled,TRUE
  welcome_watcher_enabled,TRUE
  promo_watcher_enabled,TRUE
  resume_command_enabled,TRUE
  WELCOME_ENABLED,TRUE
  ENABLE_WELCOME_HOOK,TRUE
  PROMO_ENABLED,TRUE
  ENABLE_PROMO_HOOK,TRUE
  ```


### C1C recruitment ad Config rows

Add these rows to the Mirralith workbook `Config` tab before enabling the `c1c_ad` feature:

```text
C1C_AD_TAB = C1C_AD
C1C_AD_IMAGE_RANGE = A1:V42
C1C_AD_TEXT_TAB = C1C_AD_TEXT
C1C_AD_TEXT_ROW = 2
C1C_AD_TARGET_THREAD_ID = 1324313499731755039
C1C_AD_REFRESH_DAYS = 7
```

Add the `C1C_AD_TEXT` worksheet with row 1 headers:

```text
ad_text | last_posted_at_utc | last_image_message_id | last_text_message_id | last_post_status | last_post_error | updated_at_utc
```

The bot treats row 2 as the single active recruitment ad row and resolves all state columns by header name.

**Behavior**

- Missing tab or header ⇒ all features disabled; emits one admin-ping warning in the runtime log channel.
- Missing feature row ⇒ that feature disabled; logs one admin-ping warning the first time the key is evaluated. Housekeeping/ops/onboarding loader toggles (`housekeeping_enabled`, `mirralith_overview_enabled`, `ops_permissions_enabled`, `welcome_watcher_enabled`, `promo_watcher_enabled`, `resume_command_enabled`) default to ON when absent so current deployments remain stable.
- Invalid value ⇒ disabled; logs one admin-ping warning per feature key.
- Startup continues regardless; platform services (cache, scheduler, watchdog, RBAC) are never gated.
- The `recruitment_reports` row powers the Daily Recruiter Update (scheduler + manual command). The `placement_*` rows still control stub modules that only log load state.
- Consumption sites must use the central accessor `modules.common.feature_flags.is_enabled("<feature_name>")`. Guardrails flag any FeatureToggles row that is not referenced through that accessor.

Feature enable/disable is always sourced from the FeatureToggles worksheet; ENV variables must not be used for feature flags.

**Operator flow**

1. Edit the `FeatureToggles` worksheet in the environment’s Mirralith Sheet.
2. Run `!ops reload` (or the admin bang alias) to pull the latest worksheet values and rebuild the registry.
3. Confirm the tab and headers with `!checksheet`; resolve any ⚠️ rows before retrying.

**Troubleshooting**

- Warnings mention the first role listed in `ADMIN_ROLE_IDS` and are posted to the runtime log channel.
- Verify the worksheet name matches the Config key and that headers are spelled correctly.
- Use `!ops reload` (or the Ops equivalent) to force the bot to re-read the toggles after a fix.

> **Template note:** The `.env.example` file in this directory mirrors the tables below. Treat that file as the canonical template for new deployments and update both assets together.

Doc last updated: 2026-06-08 (v0.9.8.2)
