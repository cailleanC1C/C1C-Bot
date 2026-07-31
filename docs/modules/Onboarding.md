# Onboarding Module

## Purpose & Scope
The onboarding module is the generic questionnaire engine that powers welcome and any other future intake flows. It loads question metadata from the onboarding workbook (`ONBOARDING_SHEET_ID` + `ONBOARDING_TAB`), evaluates skip/navigation rules, validates user input, and persists progress so a recruit can pause and resume. Everything in this module is Discord-agnostic: controllers emit structured state, not embeds, and downstream UX layers (Welcome, promo, future surfaces) decide how to present that state.

## Responsibilities
- **Question definitions** — read the `flow, order, qid, label, type, required, maxlen, validate, help, options, visibility_rules, nav_rules, rules` columns from the configured tab and normalise them into `Question` dataclasses (`modules/onboarding/schema.py`).
- **Rules & validation** — parse `visibility_rules`/`nav_rules` for skip logic, enforce per-question validators (regex, enumerated options, min/max lengths), and emit actionable error strings (`modules/onboarding/rules`, `modules/onboarding/validation`).
- **Session state** — track `step_index`, answered questions, derived visibility, and completion status per thread/user, including multi-device resumes.
- **Persistence** — read/write the `OnboardingSessions` tab so progress survives restarts (`shared/sheets/onboarding_sessions.py`).
- **Lifecycle logging** — emit structured diagnostics (cache refresh summaries, question counts, wizard actions, summary posted events) through the shared onboarding log helpers.
- **Summary data model** — supply the normalized answer payload (keyed by question `qid`, e.g., `w_power`, `w_hydra_diff`) that downstream UX layers format into embeds (see [`docs/modules/Welcome.md`](Welcome.md) for the summary layout).

## Non-Goals
- No Discord thread creation, embeds, panels, or button wiring — Welcome owns those UX elements.
- No recruiting-specific copywriting or wording. Labels/help text are read verbatim from Sheets.
- No placement math, ticket renames, or reservation handling — those live in Welcome/Placement modules.
- No feature toggles beyond the shared config contract; module code expects `ONBOARDING_SHEET_ID` + config rows to already exist.

## Data Model & Sheets
### Question Definition Tab (`ONBOARDING_TAB`)
- **Headers:** `flow`, `order`, `qid`, `label`, `type`, `required`, `maxlen`, `validate`, `help`, `options`, `visibility_rules`, `nav_rules`, `rules` (Config doc mirrors this schema). Rows with `flow=welcome` drive the Discord welcome dialog; other flows reuse the same engine.
- **Options:** stored as comma-separated tokens; converted into `(label, value)` tuples so both dropdowns and multi-select prompts retain sheet ordering.
- **Rules:** `visibility_rules` and `nav_rules` reference other `qid` values; parser rejects unknown IDs so skip logic does not drift.
- **Score validation:** set `validate=score` for approximate scores, or use inclusive base-unit bounds such as `validate=score:min=10000,max=1000000`. Accepted answers include compact forms (`500K`, `12.6M`, `40b`), word forms (`40 billion`), qualifiers (`about`, `over`, `~`), a trailing `+`, and full numbers (`40,000,000,000` or `40000000000`). Values are stored as canonical compact strings such as `500K`, `12.6M`, and `40B`.
- **Directive-driven behavior:** score parsing is selected only by the row's `validate` directive, never by `qid`, label, help text, order, flow, question type, or an existing regex. Exact numeric fields—including player power, account levels, ranks, counts, and IDs—continue to use their own sheet-authored validators without score-specific qualifiers, suffix words, or decimal-comma handling.

### Session Persistence (`OnboardingSessions` tab)
- **Columns (minimum required):** `thread_name`, `user_id`, `thread_id`, `panel_message_id`, `step_index`, `completed`, `completed_at`, `answers_json`, `updated_at`, `first_reminder_at`, `warning_sent_at`, `auto_closed_at` (extra columns are tolerated so long as the required ones exist).
- **ID handling:** `thread_id` and `user_id` are written as text to avoid Google Sheets’ 15-digit truncation; format these cells as *plain text* (not numbers/scientific notation) if rows look mangled.
- **Creation timing:** As soon as a welcome or promo panel is posted, the watcher writes/updates the row keyed by `thread_id` with `thread_name` (e.g., `W0603-smurf`), the first mention from the trigger message as `user_id`, the `panel_message_id`, `step_index=0`, `completed=False`, empty `answers`, and an auto-set `updated_at` timestamp. Later reminders reuse the same row via `thread_id`.
- **Upserts, not duplicates:** Persistence keys on `thread_id` only. Reminder/idle scans and wizard saves update the existing row; repeated scans must not increase the row count for a given thread.
- **`answers_json`:** compact JSON storing the latest answers keyed by `qid` (`{"w_ign": "Caillean", "w_hydra_diff": ["Hard", "Brutal"]}`, etc.).
- **Reminder timestamps:** `first_reminder_at`/`warning_sent_at`/`auto_closed_at` prevent repeated pings and track the welcome-thread lifecycle (5h nudge → 24h warning → 36h auto-close+rename to `Closed-…-NONE`).
- **Ops sanity checks:** If data looks wrong, confirm the header includes `thread_id`/`thread_name`/`updated_at`, verify IDs render as text (not rounded), and spot-check that `thread_name` matches the Discord thread for the ticket. Start investigations here when a ticket looks wrong or is missing.

### Idle watcher + scheduler visibility
- **Idle reminders:** the onboarding idle watcher polls `OnboardingSessions` as source of truth. It pings the player after 5h of inactivity, pings the recruitment coordinators + player at 24h, and auto-closes after 36h (rename to `Closed-…-NONE`, lock/archive, release any linked reservation). Welcome auto-close messages ask coordinators to remove the user; promo closes skip the removal note.
- **Scheduler overview:** `!next [component]` (admin-only) shows upcoming jobs grouped by component; the idle watcher is registered under `recruitment`.

### Ticket & Summary Mapping
- The module prepares row values for the onboarding workbook tabs consumed by welcome/placement automation:
  - **`WelcomeTickets`** (`ticket_number`, `username`, `clantag`, `date_closed`) keeps ticket metadata; onboarding helpers upsert rows so placement math can reconcile reservations later.
  - **`PromoTickets`**, `ClanList`, and other tabs reuse the same helper plumbing via `shared/sheets/onboarding.py` but live higher in the stack (watchers decide when to call them).

## Flows
1. **Startup preload** — cache scheduler registers `onboarding_questions`; `shared.cache.telemetry` refreshes it weekly or on demand (`!ops refresh onboarding_questions`). Missing headers throw `missing config key: ONBOARDING_TAB` so ops can fix the sheet.
2. **Wizard launch** — Welcome (or another UX) asks the engine for the `welcome` flow. Questions are sorted by `order` (numeric suffix + optional alpha tag) and cached per thread.
3. **Step render & answer capture**
   - Controller hydrates metadata (label, type, validators) straight from the question row.
   - `visibility_rules` determine which questions should render. `nav_rules` allow branching (skip to `w_cvc` if Siege = "yes", etc.).
   - Incoming answers are normalised (trimmed strings, canonical booleans, sorted multi-select tokens) before being stored.
   - Validation failures return sheet-authored error strings so Welcome can display consistent inline hints.
   - Inline replies (welcome and promo) delete the raw message once captured and refresh the existing wizard card via `session_status` instead of posting a duplicate panel.
4. **Persistence & resume** — after every mutation the engine writes to `OnboardingSessions`. Re-opening the wizard loads `answers_json`, rebuilds visibility maps, and resumes at the first incomplete question.
5. **Completion** — when `step_index` exceeds the last visible question the engine marks the session `completed`, freezes answers, emits summary logs (`modules/onboarding/logs.question_stats`), and hands the normalized payload to Welcome for embed formatting. Completed sessions reject restarts; staff can use `!onb resume @user` (CommandMatrix entry) to rebind panels inside a ticket thread when Discord ate the message.

## Dependencies
- **Config:** `shared.config` + `docs/ops/Config.md` define required env keys (`ONBOARDING_SHEET_ID`, optional `ONBOARDING_CONFIG_TAB`) and sheet tab overrides.
- **Sheets access:** `shared/sheets/onboarding_questions`, `shared/sheets/onboarding_sessions`, and `shared/sheets/onboarding` supply cached question rows, persistence helpers, and tab upserts. All I/O honours service-account credentials (`GSPREAD_CREDENTIALS`/`GOOGLE_SERVICE_ACCOUNT_JSON`).
- **Caches:** `shared.cache.telemetry` + cache scheduler refresh `onboarding_questions`; modules register buckets at startup so `!ops refresh onboarding_questions` works.
- **Logging:** `modules/onboarding/logs` standardises structured events (launch, resume, summary posted, errors) and ties into the runtime log channel configured in `docs/ops/Logging.md`. Failed panel opens now emit severity-appropriate levels with explicit `result`/`reason` fields so ops dashboards can distinguish errors from successful launches.
- **Tests:** `tests/onboarding/**` cover cache wiring, sheet ID enforcement, watcher placement reconciliation, and welcome dialog controllers per ADR-0022.

## Related Docs
- [`docs/modules/Welcome.md`](Welcome.md)
- [`docs/Architecture.md`](../Architecture.md)
 - [`docs/ops/Config.md`](../ops/Config.md)
 - [`docs/ops/CommandMatrix.md`](../ops/CommandMatrix.md)
- [`docs/Runbook.md`](../Runbook.md)
- [`docs/adr/ADR-0022-Module-Boundaries.md`](../adr/ADR-0022-Module-Boundaries.md)

Doc last updated: 2026-07-30 (v0.9.8.2)
