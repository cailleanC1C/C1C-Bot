# Google Sheets Read Audit

Date: 2026-07-10  
Scope: bot runtime code under `app.py`, `modules/`, `shared/`, and `packages/c1c-coreops/src` using `rg` for direct and indirect Google Sheets reads. `AUDIT/` and tests are not part of runtime scope.

## Executive summary

- **Total logical read entry-point groups found:** 38.
- **Primary quota-pressure drivers:** startup preload/cache refreshes, onboarding welcome/promo close backfill on every startup, housekeeping cleanup startup validation, reminder scheduler ticks that can refresh/read multiple reminder tables, command-triggered admin refreshes, reaction-role event reads, and repeated onboarding Config/tab/header resolution.
- **Central shared cache usage exists but is incomplete.** Cache buckets coalesce concurrent refreshes inside `cache_service.get()` background refresh, but `refresh_now()` does **not** join an in-flight refresh; overlapping startup/manual/cron refreshes can duplicate reads for the same bucket.
- **Several paths bypass `cache_service`:** onboarding ticket helpers, onboarding session store, promo/welcome ticket helpers, reaction roles, CoreOps helpseed, housekeeping cleanup/role audit, leagues config, shard tracker/server map, reservations, daily recruiter update, and direct `core.call_with_backoff(ws.get_all_values/row_values)` paths.
- **No feature behavior was intentionally changed by this PR.** The code change adds opt-in structured audit logging only, gated by `SHEETS_READ_AUDIT_LOGGING=false` by default.

## Startup read timeline

| Order | Startup source | Reads started | Why it happens | Diagnosis | Recommendation |
|---:|---|---|---|---|---|
| 1 | `app.on_ready` -> `modules.coreops.ready.on_ready` | Config/bootstrap plus registered cache preloads | Ready lifecycle initializes CoreOps and cache status | Can read buckets not immediately needed | Split critical vs delayed preload; defer low-priority buckets until after ready |
| 2 | `shared.sheets.runtime.register_cache_buckets` / feature refresh loaders | `clans`, `templates`, `clan_tags`, `onboarding_questions`, feature buckets | Registers and warms shared cache | Manual/startup and cron can overlap | Make `refresh_now()` join in-flight refresh per bucket |
| 3 | Fusion scheduler startup refresh | fusion rows and fusion event rows | Catches missed announcements/reminders | Startup refresh can occur close to cron refresh | Add scheduler de-dupe window and coalesce fusion/fusion_events refreshes |
| 4 | Reset reminder scheduler startup | reset reminder settings/state | Schedules reset reminders after boot | Can duplicate reminder tick reads | Use cache snapshot if fresh; avoid extra config read per tick |
| 5 | Housekeeping cleanup startup validation | cleanup config/ticket rows plus Discord history scan | Validates cleanup dry-run at boot although real job is daily | Startup-only validation is not necessary every boot and may scan Discord history | Make manual-only or run once per deploy/day; no Discord ops message unless actionable |
| 6 | Welcome close backfill | onboarding welcome tab full scan and ticket context checks | Recovery job tries to finalize already-closed tickets | Runs every startup; appears broad rather than cursor/recent-only | Add cursor/recent/unresolved-only bounds and summary-only app logs |
| 7 | Promo close backfill | onboarding promo tab full scan and ticket context checks | Same recovery behavior for promo close/finalization | Runs every startup and can produce noisy ops messages for old tickets | Make manual/admin or bounded recovery; old missing context should app-log summary only |
| 8 | Startup summary/embed | cache telemetry and possibly sheet status checks | Posts operational boot summary | Mostly metadata, but can trigger checksheets if called | Keep sheet-status reads out of default boot summary |

## Normal runtime read map

| # | Feature/module | File/function where read starts | Sheet source | Sheet ID config key | Tab config key | Headers/schema resolved? | cache_service? | Bucket | Trigger type | Expected frequency | Worst-case frequency | Concurrent same data? | Necessary at trigger? | Remove/delay/cache/coalesce/manual-only | Failure visibility | Current logging | Recommended change |
|---:|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | Runtime config | `shared/config.py::load_from_sheets` | Config worksheet | varies by domain/env | `CONFIG_TAB`-style key | yes, records headers | bypass | - | startup, reload command | boot + manual reload | every reload / repeated startup | yes | yes for config load | cache config with TTL and clear reload path | Discord-visible for admin reload; app-log at startup | warning/error logs | Keep, but log audit and avoid repeated reload during one lifecycle |
| 2 | Sheet Config resolution | `shared/sheets/recruitment.py::_load_config`, `shared/sheets/onboarding.py::_load_config` | domain Config tabs | `RECRUITMENT_SHEET_ID`, `ONBOARDING_SHEET_ID` | recruitment/onboarding config tab key | yes | bypass | - | startup, command, event helper | when domain helper first used | repeated per helper if local cache invalidated | yes | necessary once | cache resolved sheet/tab/header metadata with explicit invalidation | app-log-only except admin command | module logs | Add metadata TTL and single-flight resolution |
| 3 | Recruitment config | `shared/sheets/recruitment.py::_load_config` | Recruitment sheet Config | `RECRUITMENT_SHEET_ID` | `RECRUITMENT_CONFIG_TAB` | yes | bypass | - | startup, command | first recruitment use | repeated under concurrent calls | yes | yes | single-flight config load | app-log/admin command visible | warning logs | Coalesce config load |
| 4 | Recruitment clans | `shared/sheets/recruitment.py::fetch_clans` | Recruitment clan tab | `RECRUITMENT_SHEET_ID` | clan tab from Config | yes | via cache for CoreOps buckets; direct elsewhere | `clans` | startup, cron, command, button | preload + cron + refresh command | manual refresh overlapping cron/startup | yes | not always at startup | delay startup if not needed; coalesce refresh_now | Discord-visible for commands | `[refresh]` logs | Join in-flight cache refresh and avoid duplicate `clans`/availability loaders |
| 5 | Recruitment templates | `shared/sheets/recruitment.py::fetch_templates` | Recruitment templates tab | `RECRUITMENT_SHEET_ID` | templates tab from Config | yes | cache when bucketed | `templates` | startup, cron, command | preload + cron | command bursts | yes | only needed before panel rendering | lazy-load after ready or on first command | Discord-visible for command | `[refresh]` logs | Lazy preload or lower priority |
| 6 | Clan tags | `shared/sheets/recruitment.py::fetch_clan_tags` / feature bucket | Recruitment clan list | `RECRUITMENT_SHEET_ID` | clan list tab from Config | yes | cache | `clan_tags` | startup, cron, command | preload + TTL | overlapping refreshes | yes | not always startup-critical | delay; coalesce | app-log unless command | `[refresh]` logs | Keep cached; avoid separate duplicate clan reads |
| 7 | Availability/search | `modules/recruitment/availability.py`, `modules/recruitment/search.py` | recruitment cache/sheet | `RECRUITMENT_SHEET_ID` | Config-derived | yes | mixed | `clans` sometimes | command, button | user/admin commands | can spike with button use | yes | yes for fresh command results | serve stale cache with manual refresh option | Discord-visible | command logs | Ensure command paths use snapshot rather than direct fetch |
| 8 | Daily recruiter update | `modules/recruitment/reporting/daily_recruiter_update.py` | prepared statistics tab | recruitment/shared sheet | Config-derived statistics tab | yes | bypass | - | cron | daily | retry loop + manual run | low | yes | cache daily read result for report window | app-log/ops summary | cron logs | Keep cron-only; do not run at startup |
| 9 | Onboarding config | `shared/sheets/onboarding.py::_load_config` | Onboarding Config | `ONBOARDING_SHEET_ID` | onboarding config tab | yes | bypass | - | startup, event, command | first onboarding use | repeated within seconds under ticket events | yes | necessary once | single-flight/TTL metadata cache | app-log; command visible | repeated resolution logs | Priority: cache/coalesce tab and header resolution |
| 10 | Onboarding questions | `shared/sheets/onboarding_questions.py::fetch_question_rows_async` | Onboarding questions tab | `ONBOARDING_SHEET_ID` | question tab from Config | yes | cache | `onboarding_questions` | startup, cron, command | weekly TTL + preload | manual/startup overlap | yes | not startup-critical unless panel immediate | delay until first panel or after ready | Discord-visible for panel failure | `[refresh]` logs | Lazy-load or delayed preload |
| 11 | Welcome watcher rows | `shared/sheets/onboarding.py::find/list/update welcome`, `modules/onboarding/watcher_welcome.py` | Welcome tickets tab | `ONBOARDING_SHEET_ID` | welcome tab from Config | yes | bypass | - | Discord event listener, backfill, admin | per ticket close/update | event spikes + startup backfill full scan | yes | event yes; startup no | cache row index; bound backfill | app-log for old/missing context; Discord only actionable | noisy watcher logs | Add per-run row index and startup backfill limits |
| 12 | Promo watcher rows | `shared/sheets/onboarding.py::find/list/update promo`, `modules/onboarding/watcher_promo.py` | Promo tickets tab | `ONBOARDING_SHEET_ID` | promo tab from Config | yes | bypass | - | Discord event listener, backfill, admin | per promo close/update | startup scan + close events | yes | event yes; startup broad no | cursor/recent/unresolved-only; manual old backfill | app-log summary | noisy ops messages | Make old missing context summary-only |
| 13 | Onboarding close backfill | `modules/onboarding/watcher_welcome.py`, `modules/onboarding/watcher_promo.py` startup hooks | welcome/promo tabs | `ONBOARDING_SHEET_ID` | welcome/promo tab keys | yes | bypass | - | startup backfill/recovery | every boot today | full sheet scan every restart | yes | not every startup | make daily/manual, cursor-based, recent-only | app-log summary | Discord ops noise | Highest priority removal from startup |
| 14 | Onboarding session store | `shared/sheets/onboarding_sessions.py::load/save/list` | session tab | `ONBOARDING_SHEET_ID` | session tab key | yes | bypass | - | Discord interaction, commands, idle watcher | per wizard step/session lookup | high during onboarding activity | yes | yes, but not all need full sheet | cache active sessions with write-through consistency | Discord-visible to user only for active flow | limited | Replace full-table reads with cached active session index |
| 15 | Welcome ticket helper | `shared/sheets/welcome_tickets.py::read_rows` | welcome tickets | `ONBOARDING_SHEET_ID` | configured tab | yes | bypass | - | watcher/admin | per helper use | repeated under events | yes | sometimes | fold into onboarding row cache | app-log | limited | Remove duplicate helper or route through shared index |
| 16 | Promo ticket helper | `shared/sheets/promo_tickets.py::read_rows` | promo tickets | `ONBOARDING_SHEET_ID` | configured tab | yes | bypass | - | watcher/admin | per helper use | repeated under events | yes | sometimes | fold into onboarding row cache | app-log | limited | Same as welcome helper |
| 17 | Housekeeping cleanup | `modules/housekeeping/cleanup.py::startup_validation/run_cleanup` | cleanup/ticket config + Discord history | config-driven | cleanup config keys | yes | bypass | - | startup, daily cron, manual admin | daily expected | every startup + daily + manual | possible | startup validation no | make validation manual/daily-only; avoid history scan at startup | app-log summary; Discord only real cleanup/action | ops dry-run logs | Remove startup dry-run/history scan |
| 18 | Housekeeping role audit | `modules/housekeeping/role_audit.py` | role/audit config | config-driven | config tab | yes | bypass | - | cron/manual | scheduled/manual | overlapping admin+cron | possible | scheduled yes | cache config snapshot | ops summary | audit logs | Keep manual/cron only |
| 19 | C1C ad housekeeping | `modules/housekeeping/c1c_ad.py` | ad config/schedule | config-driven | config tab | yes | bypass/mixed | - | cron/manual | scheduled | low | possible | yes | cache config | app-log/ops summary | logs | Keep, use config snapshot |
| 20 | Fusion config/events | `shared/sheets/fusion.py::load_*` | Fusion/fusion events tabs | fusion sheet config key | fusion tab/event tab keys | yes | cache/mixed | `fusion`, `fusion_events` if registered | startup, cron, manual, reminder tick | scheduled + reminders | startup/manual close to cron duplicates | yes | tick should use cache | coalesce fusion + events and avoid direct tick reads | Discord-visible for admin; app-log for tick | `[refresh]`, reminder logs | Single-flight refresh and tick snapshot reuse |
| 21 | Fusion reminders | `modules/community/fusion/reminders.py` | fusion rows, events, dedupe | fusion/reminder sheets | Config-derived | yes | mixed | fusion buckets | cron scheduler | every tick | tick can read config/events/dedupe separately | yes | yes but cache acceptable | cache settings/events; decide dedupe consistency with write-through | app-log except sent reminder failures | reminder logs | Avoid per-tick config read; cache dedupe with strict write/read update |
| 22 | Fusion role cleanup | `modules/community/fusion/role_cleanup.py` | fusion rows/event state | fusion sheet | Config-derived | yes | mixed | fusion bucket maybe | cron/manual | scheduled | overlap with fusion reminder | yes | yes scheduled | share fusion snapshot | app-log/ops summary | logs | Do not independently refresh same rows |
| 23 | Reset reminders | `modules/community/reset_reminders/scheduler.py` | reminder settings/state | `REMINDER_SHEET_ID` or config | tab config | yes | mixed/bypass | - | startup, cron | scheduled tick | startup + tick duplicate | yes | tick yes | cache settings; avoid startup unless scheduling requires | app-log/ops summary | scheduler logs | Use cached config and single tick runner |
| 24 | Reaction roles | `shared/sheets/reaction_roles.py`, `modules/community/reaction_roles.py` | reaction-role mapping tab | configured sheet | configured tab | yes | bypass | - | reaction event, startup command | event-driven | can spike per reaction add/remove | yes | event needs mapping, not live read | cache mapping and refresh on admin command/TTL | app-log; user not visible | limited | Critical: no live sheet read per reaction; coalesce refresh |
| 25 | Leagues autoposter | `modules/community/leagues/config.py/service.py/cog.py` | C1C_Leagues Config/content | `LEAGUES_SHEET_ID` | `LEAGUES_CONFIG_TAB` and content tabs | yes | bypass | - | startup, cron, command | scheduled posting | command+cron overlap | possible | yes for post | cache league specs with TTL and manual refresh | Discord-visible for admin command | logs | Register cache bucket or use single-flight loader |
| 26 | Shard tracker | `modules/community/shard_tracker/data.py/cog.py` | shard tracker sheet | configured sheet | configured tab | yes | bypass | - | command/button/cron | user/admin interactions | button bursts | yes | yes | cache tracker rows per TTL | Discord-visible | command logs | Add cache/coalescing for read-only tracker data |
| 27 | WhoWeAre/Mirralith | Mirralith-related recruitment/daily report paths | shared Mirralith spreadsheet | configured recruitment/shared key | Config-derived | yes | bypass/mixed | - | cron/command | daily or command | low unless command spam | possible | yes | cache prepared report rows | app-log/Discord for command | logs | Confirm ownership; keep config-driven |
| 28 | Reservations | `shared/sheets/reservations.py::load_reservations`, `modules/placement/reservations.py` | reservations tab | placement/recruitment sheet key | reservations tab key | yes | bypass for writes/reads | - | command/event | reservation commands | command bursts | yes | yes | cache active reservations with write-through | Discord-visible command | command logs | Avoid full read after every write if local update safe |
| 29 | Server map | `modules/ops/server_map_state.py::load_*` | server map sheet | ops/server map sheet key | server map tab key | yes | bypass | - | startup/admin command | occasional | low | possible | startup maybe not | delay until command if not needed | admin visible | logs | Lazy-load unless startup summary needs it |
| 30 | Permissions UI | `modules/ops/permissions_ui.py`, `cluster_role_map.py` | config/role maps | ops sheet/config | Config-derived | yes | bypass/mixed | - | command/button | admin interactions | button bursts | yes | yes | cache role maps for interaction session | Discord-visible admin | command logs | Use session-local cache |
| 31 | CoreOps checksheet | `packages/c1c-coreops/src/c1c_coreops/cog.py` | configured sheet | operator-provided/configured | operator tab | yes | bypass | - | command/manual admin | on demand | admin spam | possible | yes | cooldown/manual only | Discord-visible | command logs | Keep manual; cooldown is enough |
| 32 | CoreOps helpseed | `packages/c1c-coreops/src/c1c_coreops/cog.py::_helpseed` | help registry | CoreOps/config sheet key | help registry tab key | yes | bypass | - | command/manual admin | rare | low | no | yes | no startup execution | Discord-visible | command logs | Keep manual-only |
| 33 | Feature toggles/refresh | `shared/sheets/feature_refresh.py` | feature/config tabs | domain sheet IDs | feature tab keys | yes | cache | feature buckets | startup, cron, reload | preload + cron | overlaps reload | yes | startup maybe | coalesce and don't refresh twice after reload | app-log/admin visible | refresh logs | Single-flight refresh by bucket |
| 34 | Milestones config | `shared/sheets/milestones_config.py` | milestones/config tabs | `MILESTONES_SHEET_ID` | milestones config tab | yes | bypass | - | startup/cron/commands | scheduled feature use | overlap with fusion/reminders | possible | yes when feature active | cache milestones config | app-log | warnings | Add cache bucket if frequently read |
| 35 | Export utils | `shared/sheets/export_utils.py` | arbitrary configured tab | caller-provided | caller-provided | yes | bypass | - | command/manual | on demand | low | possible | yes | keep manual-only | Discord/admin visible | logs | No startup use |
| 36 | Direct worksheet reads | any `core.call_with_backoff(ws.get_all_values/row_values)` | worksheet object | caller-resolved | caller-resolved | caller-specific | bypass | - | mixed | mixed | high if event path | yes | varies | replace with named helpers/cache buckets | varies | inconsistent | Route logical reads through shared helpers for auditability |
| 37 | Async direct worksheet reads | `async_core.acall_with_backoff(worksheet.get_all_values/row_values)` | worksheet object | caller-resolved | caller-resolved | caller-specific | bypass | - | mixed | mixed | high in async commands | yes | varies | same as direct worksheet reads | varies | inconsistent | Wrap with named logical read helper |
| 38 | Raw range reads | `core.sheets_read/asheets_read` | explicit range | caller-resolved | embedded A1/tab | no or caller-specific | bypass | - | diagnostics/manual/features | occasional | unknown | possible | varies | avoid broad range reads in runtime | app-log/admin | limited | Require caller component metadata in audit call sites |

## Reads caused by Discord events

- Onboarding welcome close/update listeners can read welcome rows and resolve onboarding Config/tab/header metadata during normal ticket lifecycle events.
- Onboarding promo close/update listeners can read promo rows during ticket events.
- Onboarding session interactions can read the full session table for wizard step/session lookup.
- Reaction-role add/remove events can read mapping rows unless the mapping is already cached by module state.
- Recruitment/search buttons and panels can read clan/template/availability data if they bypass a fresh cache snapshot.
- Shard tracker buttons/views and permissions UI buttons can read their configured sheet-backed maps.

## Reads caused by cron/schedulers

- Shared cache refresh scheduler for registered buckets (`clans`, `templates`, `clan_tags`, `onboarding_questions`, feature buckets).
- Fusion announcement/reminder scheduler and fusion role cleanup.
- Reset reminder scheduler.
- Daily recruiter update.
- Housekeeping cleanup daily job and role/ad audit jobs.
- Leagues autoposter.
- CoreOps daily summary/status if configured to inspect sheet/cache status.

## Reads caused by commands/buttons

- `!ops refresh <bucket>` / `!ops refresh all` and CoreOps health/checksheet/config/helpseed commands.
- Recruitment search, member/recruiter panel actions, welcome refresh, and availability commands.
- Onboarding admin resume/finish-placement and welcome/promo panel actions.
- Leagues, shard tracker, permissions UI, reservation commands, and export/admin diagnostics.

## Reads that bypass `cache_service`

- Config loads in `shared/config.py` and domain Config resolvers in `shared/sheets/recruitment.py` and `shared/sheets/onboarding.py`.
- Onboarding welcome/promo/session helpers in `shared/sheets/onboarding.py`, `shared/sheets/onboarding_sessions.py`, `shared/sheets/welcome_tickets.py`, and `shared/sheets/promo_tickets.py`.
- Reaction roles in `shared/sheets/reaction_roles.py`.
- Leagues, shard tracker, server map, permissions/role maps, reservations, daily recruiter update, export utilities, CoreOps checksheet/helpseed.
- Any direct `core.call_with_backoff(ws.get_all_values)`, `core.call_with_backoff(ws.row_values)`, `async_core.acall_with_backoff(worksheet.get_all_values)`, and raw `core.sheets_read/asheets_read` use.

## Duplicate or overlapping reads

1. `cache_service.refresh_now()` starts a new refresh even if the same bucket is already refreshing; startup, manual refresh, and cron can overlap.
2. Fusion and fusion_events can refresh as startup/manual work near scheduled cron ticks, then reminder ticks can read related settings/dedupe separately.
3. Onboarding welcome/promo startup backfill reads the same tabs that event listeners may read moments later.
4. Onboarding Config/tab/header resolution can repeat within seconds because multiple helpers resolve independently.
5. Reaction-role mapping can be read per reaction if module state is cold/stale.
6. Reservations and onboarding session writes often perform a full read to find/update a row immediately before or after write operations.

## Reads that should not happen at startup

- Housekeeping cleanup `startup_validation` dry-run, especially if it scans Discord history. The scheduled daily cleanup can validate immediately before real work; boot should not pay that cost.
- Welcome and promo close backfill over full historical tabs. Startup recovery should be bounded by cursor/recent/unresolved rows or be manual-only.
- Low-priority cache buckets not needed before the bot can safely answer Discord events, such as onboarding questions, templates, and some reporting/league specs.
- Any checksheet/helpseed/export/admin diagnostics.

## Specific production behavior diagnosis

- **Housekeeping startup validation:** current behavior indicates a boot hook runs dry-run validation every startup while the actual cleanup is daily. That means quota and Discord history scan cost is paid on every deploy/restart without performing cleanup. Make this manual-only or daily-job-local validation.
- **Cleanup sheet/history scope:** if startup validation resolves cleanup Sheets and scans Discord history, both are unnecessary at startup. Startup should only register the scheduler and log config presence at most.
- **Welcome/promo close backfill:** these jobs run on every startup as recovery. They appear broad enough to find old tickets with missing context. Backfill should be cursor-based, recent-only, and unresolved-only; otherwise it becomes a full-table boot tax.
- **Old missing ticket context:** old tickets with missing context should produce app-log summaries and metrics, not per-ticket Discord ops messages. Discord ops should be reserved for actionable current failures.
- **Repeated onboarding resolution:** onboarding helpers independently resolve Config, tabs, and headers; concurrent ticket events/backfill amplify this. Add single-flight metadata resolution and reuse per run.
- **Fusion/fusion_events close refreshes:** startup/manual refresh and cron are not coalesced at `refresh_now()`, so the same bucket can refresh twice when schedules align.
- **Reminder ticks:** reminder paths should use cached Config/fusion/event/settings snapshots. Dedupe state can be cached if writes update the in-memory state before the next tick and failures invalidate/refetch.
- **Reaction role reads:** reaction events must not perform live sheet reads in the hot path; cache mapping with TTL/manual refresh and return safe no-op on unavailable cache.
- **Startup preload:** classify buckets as critical/immediate vs warm-later; do not preload buckets used only by commands or daily cron.

## Temporary structured audit logging added

Set `SHEETS_READ_AUDIT_LOGGING=true` to emit one structured app log per logical read wrapped by shared helpers/cache service. The default in `docs/ops/.env.example` is `false`.

Logged fields:

- `component`
- `operation`
- `sheet_source`
- `tab_config_key`
- `cache_bucket`
- `trigger`
- `caller`
- `cache`
- `duration_ms`
- `result`
- `error_type`

Coverage added by this PR:

- `shared.sheets.core.fetch_records`, `afetch_records`, `fetch_values`, and `afetch_values`.
- `shared.sheets.cache_service.get` cache hit/miss/stale-return paths.
- `shared.sheets.cache_service` refresh and retry loader paths.

Known remaining instrumentation gap: direct worksheet reads through `core.call_with_backoff(ws.get_all_values/row_values)` and `async_core.acall_with_backoff(worksheet.get_all_values/row_values)` do not always expose component/tab metadata to the central wrapper. Those are identified above and should be migrated to named helper reads during remediation.

## Prioritized fix plan

1. **Stop startup backfill/validation waste:** make housekeeping cleanup startup dry-run manual-only or daily-only; bound onboarding welcome/promo close backfill by cursor/recent/unresolved rows.
2. **Add cache single-flight:** make `cache_service.refresh_now()` join an existing in-flight bucket refresh; add a short de-dupe window for startup/manual/cron refresh collisions.
3. **Cache/coalesce metadata:** single-flight onboarding/recruitment Config, tab, and header resolution. Include explicit invalidation on admin reload.
4. **Protect Discord event paths:** ensure reaction-role, onboarding session, and ticket close listeners use cached/indexed sheet data and never full-table read per event.
5. **Reminder snapshot reuse:** make fusion/reset reminder ticks use existing cache snapshots; cache dedupe with write-through consistency or batch reads per tick.
6. **Move noisy Discord ops messages:** summarize old/missing-context backfill issues in app logs; send Discord ops messages only for current actionable failures.
7. **Delay low-priority preload:** separate critical startup buckets from warm-later/cache-on-first-use buckets.
8. **Complete instrumentation:** migrate direct worksheet reads into named logical read helpers with component/trigger metadata so audit logs cover all paths without guessing.
