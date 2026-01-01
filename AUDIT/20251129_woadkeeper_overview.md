# The Woadkeeper Technical Overview (Audit)

## 4.1. Overview
The Woadkeeper is the unified Discord bot in this repo that merges prior recruitment, welcome/onboarding, placement, and operational utilities into one cog-based application. It spans member-facing recruitment search/panels, threaded onboarding dialogs (welcome and promo tickets), reservation upkeep, shard tracking, and admin operations such as server map refreshes and sheet-driven reports.【F:README.md†L4-L41】【F:modules/coreops/ready.py†L11-L20】

## 4.2. Module / Cog Inventory
| Module / Cog | File path | Purpose | User group(s) | Depends on Sheets? | Depends on ENV? |
| --- | --- | --- | --- | --- | --- |
| `AppAdmin` | `cogs/app_admin.py` | Admin utilities: ping reaction, server map refresh command, Who We Are roster rendering via sheet data and channel resolution helpers. | Admin | Recruitment config/role map sheet for roster tab; server map uses cached guild layout (no direct sheet read). | Yes – server map toggle (`SERVER_MAP`), channel IDs (`WHO_WE_ARE_CHANNEL_ID`), feature flags, logging channel. |
| `WelcomeBridge` | `cogs/recruitment_welcome.py` | Staff command `!welcome` to post templated welcomes and admin `!welcome-refresh` to reload cached templates. Delegates to recruitment welcome service. | Staff/Admin | Uses cached welcome templates from recruitment sheets. | Yes – command gating via roles; sheet caches resolved via config/env sheet IDs. |
| `RecruitmentReporting` | `cogs/recruitment_reporting.py` | Admin `!report recruiters` posts Daily Recruiter Update and logs results. | Admin | Daily report built from recruitment sheet data. | Yes – feature flag and sheet IDs; channel targets from env. |
| Onboarding `WelcomeController` | `modules/onboarding/controllers/welcome_controller.py` | Handles welcome dialog UI, validation, and summary embed generation for welcome tickets. | Members/Staff | Loads onboarding questions from onboarding sheet tab; writes session data. | Yes – welcome/promo toggles, channel IDs for welcome/promo threads. |
| Onboarding `PromoController` | `modules/onboarding/controllers/promo_controller.py` | Variant controller for promo flows (returning/move/lead). | Members/Staff | Same onboarding questions source with promo flow schema selection. | Yes – promo toggles, promo channel env. |
| Onboarding flow router | `modules/onboarding/welcome_flow.py` | Resolves thread scope (welcome vs promo), gates feature toggles, fetches questions, and runs the appropriate controller. | Internal | Reads onboarding sheet question cache. | Yes – feature flags (`welcome_dialog`, `promo_enabled`), Ticket Tool ID, channel allow-list. |
| Welcome watcher | `modules/onboarding/watcher_welcome.py` | Registers persistent welcome panel, listens to welcome threads, posts panels, reminders, summaries, and writes welcome ticket data back to sheets/reservations. | Staff/Members/Internal | Reads onboarding sheet tabs (`WELCOME_TICKETS_TAB`), recruitment/reservations sheets; writes ticket outcomes/reservations. | Yes – `WELCOME_CHANNEL_ID`, `TICKET_TOOL_BOT_ID`, feature toggles (`recruitment_welcome`, `welcome_dialog`), coordinator/guardian role IDs. |
| Promo watcher | `modules/onboarding/watcher_promo.py` | Hooks promo ticket threads, posts panel with clan selection, captures closure markers, and records promo ticket rows. | Staff/Members/Internal | Writes promo ticket rows to onboarding sheet (`PROMO_TICKETS_TAB`); uses clan tags cache. | Yes – `PROMO_CHANNEL_ID`, `TICKET_TOOL_BOT_ID`, promo toggles. |
| Onboarding session store | `modules/onboarding/session_store.py` | In-memory session tracking for dialog progress, pending steps, and preview message anchors. | Internal | No sheet access; controllers use it with sheet-loaded questions. | No – uses runtime only. |
| Onboarding panels/UI | `modules/onboarding/ui/*` (panels, views, summary_embed) | Renders interactive panel, summary embeds, retry UI for welcome/promo flows. | Members/Staff | Depends on question schema from sheets. | Yes – uses thread/channel context; feature toggles. |
| Onboarding startup wiring | `modules/coreops/ready.py` | Registers onboarding persistent views and sets up welcome/promo watchers on ready. | Internal | No | No – relies on watcher modules’ env toggles. |
| Recruitment welcome service | `modules/recruitment/welcome.py` | Builds templated welcome messages using cached templates and sends to threads; refreshes cache. | Staff | Recruitment templates sheet. | Yes – sheet IDs, role/channel config. |
| Recruitment reporting | `modules/recruitment/reporting/daily_recruiter_update.py` | Generates Daily Recruiter Update embeds and posts to configured destination. | Admin/Staff | Recruitment sheet (FeatureToggles + report tabs). | Yes – report destination env, feature toggle. |
| Reservation jobs | `modules/placement/reservation_jobs.py` | Scheduled reminders and auto-release tasks for reservation ledger; logs and recomputes availability. | Admin/Recruiter | Reservations sheet ledger. | Yes – recruiter channel/thread env IDs, feature toggles. |
| Permissions UI | `modules/ops/permissions_ui.py` | Admin `!perm` interactive permissions UI for role overwrite management. | Admin | No | Yes – uses permissions blacklist env keys. |
| Cleanup watcher | `modules/ops/cleanup_watcher.py` | Periodic cleanup of configured threads (bulk delete old panel messages). | Admin/Internal | No | Yes – `CLEANUP_THREAD_IDS`, `CLEANUP_AGE_HOURS`. |
| Keepalive helper | `modules/common/keepalive.py` | Periodic HTTP pings to keep service alive; configurable URL/interval. | Internal | No | Yes – `KEEPALIVE_URL`, `KEEPALIVE_INTERVAL`, `RENDER_EXTERNAL_URL`, `PORT`. |
| Server map utilities | `modules/ops/server_map*.py` | Builds automated server map posts with category/channel filtering. | Admin/Internal | No direct sheet dependency. | Yes – server map toggles and channel IDs. |

## 4.3. Admin Commands
- **`!ping`** (`AppAdmin.ping` in `cogs/app_admin.py`): Admin-only reaction check to confirm responsiveness; reacts with 🏓 and ignores reaction failures.【F:cogs/app_admin.py†L54-L74】
- **`!servermap refresh`** (`AppAdmin.servermap_refresh`): Admin-only; refreshes server-map channel immediately. Requires `SERVER_MAP` feature flag; posts success/failure replies and logs result.【F:cogs/app_admin.py†L75-L129】
- **`!whoweare`** (`AppAdmin.whoweare`): Admin-only; renders Who We Are roster from recruitment sheet role map tab into configured channel, with cleanup of previous bot posts and logging.【F:cogs/app_admin.py†L130-L205】
- **`!welcome-refresh`** (`WelcomeBridge.welcome_refresh`): Admin-only; reloads cached welcome templates for staff command parity.【F:cogs/recruitment_welcome.py†L59-L70】
- **`!report recruiters`** (`RecruitmentReporting.report_group`): Admin-only; posts Daily Recruiter Update if the feature toggle is enabled and logs outcome; replies with usage if invoked incorrectly.【F:cogs/recruitment_reporting.py†L21-L65】
- **`!perm`** (`PermissionsUICog.perm`): Admin-only; launches the interactive Permissions UI for role overwrites.【F:modules/ops/permissions_ui.py†L900-L934】

**Staff/Restricted operational commands**
- **`!welcome`** (`WelcomeBridge.welcome`): Staff-only (CoreOps staff/admin roles); posts templated welcome message for specified clan and optional note, using recruitment templates cache.【F:cogs/recruitment_welcome.py†L39-L57】

**Diagnostic/maintenance watchers (no explicit commands)**
- Cleanup watcher, keepalive helper, and reservation jobs operate in the background (see §4.5.3); no direct commands but rely on env configuration.

## 4.4. Configuration Map
### 1) ENV variables
- `DISCORD_TOKEN`, `GSPREAD_CREDENTIALS`, `RECRUITMENT_SHEET_ID`, `ONBOARDING_SHEET_ID`, `PROMO_CHANNEL_ID`, `WELCOME_CHANNEL_ID`, `WELCOME_GENERAL_CHANNEL_ID`, `RECRUITERS_CHANNEL_ID`, `RECRUITERS_THREAD_ID`, `ROLEMAP_CHANNEL_ID`, `WHO_WE_ARE_CHANNEL_ID` – channel/ID wiring surfaced via config accessors used by onboarding watchers, recruitment commands, and roster rendering.【F:shared/config.py†L700-L738】
- `SERVER_MAP` toggle and related `SERVER_MAP_CHANNEL_ID` (feature flag from sheet but command checks env flag), `KEEPALIVE_URL`/`KEEPALIVE_INTERVAL`/`RENDER_EXTERNAL_URL`/`PORT` for keepalive pings.【F:cogs/app_admin.py†L99-L128】【F:modules/common/keepalive.py†L21-L120】
- Reservation/cleanup controls such as `CLEANUP_THREAD_IDS`, `CLEANUP_AGE_HOURS`, recruiter role/channel IDs for reservation jobs, and Ticket Tool bot ID gating watchers (via shared config getters).【F:modules/ops/cleanup_watcher.py†L10-L52】【F:modules/onboarding/watcher_welcome.py†L1275-L1287】

### 2) Sheets
- **Recruitment sheet**: Feature toggles (FeatureToggles tab) powering recruitment_welcome, server map, promo/welcome hooks; welcome templates; role map tab for `!whoweare`; Daily Recruiter Update source data.
- **Onboarding sheet**: Question definitions per flow, welcome/promo ticket log tabs (`WELCOME_TICKETS_TAB`, `PROMO_TICKETS_TAB`), onboarding sessions tab; schema hash loaded for dialogs.【F:modules/onboarding/welcome_flow.py†L37-L115】
- **Reservations sheet**: Reservation ledger read/write for reminders and auto-release logic; interacts with welcome ticket parsing for thread names.【F:modules/placement/reservation_jobs.py†L1-L65】

### 3) Local config files
- No local permission allow-list files; permissions are managed via the Permissions UI.

## 4.5. Flow Descriptions
### 4.5.1. Welcome Flow
1. **Trigger**: Ticket Tool welcome thread creation or manual 🧭 reaction/panel interaction in welcome channel; watcher checks `WELCOME_CHANNEL_ID` and toggles `welcome_dialog` and `recruitment_welcome` before registering persistent panel view.【F:modules/onboarding/watcher_welcome.py†L1275-L1344】
2. **Flow resolution**: `resolve_onboarding_flow` inspects thread parent scope; welcome threads map to `welcome` flow, promo threads parsed for ticket codes; errors logged with scope gate result.【F:modules/onboarding/welcome_flow.py†L37-L115】
3. **Question loading**: Uses `shared.sheets.onboarding_questions` cache to fetch questions and schema hash for the selected flow; errors log schema load failure.【F:modules/onboarding/welcome_flow.py†L102-L188】
4. **Dialog handling**: `WelcomeController` (welcome) or `PromoController` (promo) runs the dialog, leveraging session store for state, validation rules, and inline panel UI (text/select/modal) for answers and `Next` navigation.【F:modules/onboarding/controllers/welcome_controller.py†L1-L120】【F:modules/onboarding/session_store.py†L1-L70】
5. **Answer persistence**: Sessions track answers in memory; watcher writes ticket summaries to onboarding sheet tabs and updates reservations when appropriate (reserved/closed thread renaming helpers).【F:modules/onboarding/watcher_welcome.py†L1805-L2483】
6. **Summary generation**: UI summary embed builders craft final recap; retry view available if summary send fails; fallback embed handles exceptions.【F:modules/onboarding/controllers/welcome_controller.py†L10-L30】【F:modules/onboarding/ui/summary_embed.py†L1-L200】
7. **Thread closure/rename**: Helper functions build `Res-`/`Closed-` thread names and detect closure markers; watcher handles reminders, warnings, and auto-close after inactivity thresholds.【F:modules/onboarding/watcher_welcome.py†L53-L120】【F:modules/onboarding/watcher_welcome.py†L1805-L2483】
8. **Fallbacks**: Soft notifications sent on scope/feature gate failures; schema load/target lookup exceptions logged; manual reaction path shares same entrypoint to prevent divergence.【F:modules/onboarding/welcome_flow.py†L96-L188】【F:modules/onboarding/watcher_welcome.py†L146-L220】

### 4.5.2. Promo / Move / Leadership Flow
1. **Trigger**: Promo ticket threads in configured promo channel (`PROMO_CHANNEL_ID`) containing `<!-- trigger:promo.* -->` markers or Ticket Tool owner creation; watcher validates feature toggles `promo_enabled` and `enable_promo_hook`.【F:modules/onboarding/watcher_promo.py†L700-L744】
2. **Flow detection**: Thread name parsed (`R####/M####/L####` prefixes) to determine flow (`promo.r`, `promo.m`, `promo.l`), ticket metadata captured for sheet rows.【F:modules/onboarding/watcher_promo.py†L1-L70】
3. **Panel**: Posts open questions panel with clan tag select; uses shared panel renderer and clan tag cache; prompts user to select a clan tag when missing.【F:modules/onboarding/watcher_promo.py†L1-L120】
4. **Dialog processing**: Reuses onboarding dialog engine via `welcome_flow.start_welcome_dialog`, loading promo questions and schema hash; controller manages answers and validation similar to welcome flow.【F:modules/onboarding/welcome_flow.py†L62-L188】
5. **Summary & logging**: Promo watcher records ticket closure markers (`ticket closed`), updates onboarding sheet promo tab, and logs outcomes; supports clan tag selection updates before closure.【F:modules/onboarding/watcher_promo.py†L120-L250】【F:modules/onboarding/watcher_promo.py†L300-L380】
6. **Thread handling**: Detects archive/lock transitions to treat as closed; preserves reopen detection to avoid double-posting; uses ticket code parsing to rename or annotate threads as needed.【F:modules/onboarding/watcher_promo.py†L41-L120】

### 4.5.3. Background Jobs / Watchers
- **Welcome watcher**: Registers persistent panel view on ready, monitors welcome threads for panel interactions, reminders (3h/5h warnings), auto-close after 36h, and posts summaries/reservation updates; backed by reminder task scheduler started in setup.【F:modules/onboarding/watcher_welcome.py†L1275-L1344】【F:modules/onboarding/watcher_welcome.py†L2460-L2483】
- **Promo watcher**: Listens for promo ticket messages/closures in promo channel; posts panel and records ticket rows when toggles enabled.【F:modules/onboarding/watcher_promo.py†L700-L744】
- **Reservation jobs**: Daily reminder and auto-release tasks for reservations ledger; iterates due rows, posts reminders to recruiter channel, recomputes availability context, and logs releases.【F:modules/placement/reservation_jobs.py†L1-L120】
- **Cleanup watcher**: Deletes old messages in configured threads on a schedule using env-configured thread IDs and age threshold.【F:modules/ops/cleanup_watcher.py†L10-L80】
- **Keepalive**: Periodic HTTP pings using env-configured URL/interval or Render URL+port fallback to keep service responsive.【F:modules/common/keepalive.py†L21-L120】

## 4.6. Other User-Facing Features
- **Server map**: Admin `!servermap refresh` rebuilds #server-map post using live guild structure; depends on FeatureToggles flag and channel env ID.【F:cogs/app_admin.py†L75-L129】
- **Who We Are roster**: Admin `!whoweare` posts role map roster derived from recruitment sheet into configured channel, cleaning old posts for readability.【F:cogs/app_admin.py†L130-L205】
- **Shard tracker**: Member-facing shard tracking lives under `modules/community/shard_tracker` (not detailed here) and is part of Woadkeeper per README scope; operates in member threads (out of onboarding scope).【F:README.md†L14-L24】

## 4.7. Open Questions / Ambiguities
- Promo watcher relies on HTML trigger comments and thread name parsing; unclear whether Ticket Tool always injects the trigger markers in all environments—worth validating against production channel history.【F:modules/onboarding/watcher_promo.py†L1-L70】
- Reservation job feature gating spans multiple keys (`FEATURE_RESERVATIONS`, `placement_reservations`), suggesting legacy toggle aliases; confirm which sheet keys remain authoritative before altering reminders/autorelease cadence.【F:modules/placement/reservation_jobs.py†L13-L24】
- Welcome watcher writes reservation updates and ticket logs; interplay between manual `!welcome` command and automated dialog summaries may require further parity checks (not visible in this audit).【F:cogs/recruitment_welcome.py†L39-L70】【F:modules/onboarding/watcher_welcome.py†L1805-L2483】
