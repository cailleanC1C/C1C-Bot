# Phase 7 Audit — Welcome Dialog Modernization (🧭 Fallback)

## 1) Inventory — Keep / Adapt / Retire
| Path | Status | Reason / Notes |
| --- | --- | --- |
| `modules/onboarding/watcher_welcome.py` | ADAPT | Shared watcher base already logs thread closures; needs new dialog trigger + 🧭 fallback handling layered on existing closure flow. 【F:modules/onboarding/watcher_welcome.py†L70-L175】 |
| `modules/onboarding/watcher_promo.py` | ADAPT | Mirrors welcome watcher for promo threads; reuse structure but extend with dialog + manual fallback behavior tied to promo parent. 【F:modules/onboarding/watcher_promo.py†L70-L170】 |

## 2) Config sources map
- **welcome_dialog**
  - **Source:** FeatureToggles sheet via `modules.common.feature_flags.refresh()` loading `feature_name`/`enabled` columns. 【F:modules/common/feature_flags.py†L128-L228】
  - **Type:** `bool` (fail-closed, parsed through `_parse_enabled_value`). 【F:modules/common/feature_flags.py†L48-L206】
  - **Consumption sites:** Toggle queried through `feature_flags.is_enabled("welcome_dialog")`; no onboarding watcher uses it yet (future hook point). 【F:modules/common/feature_flags.py†L256-L266】

- **PROMO_CHANNEL_ID**
  - **Source:** Environment variable, loaded during `_load_config()` and exposed via `_optional_id("PROMO_CHANNEL_ID")`. 【F:shared/config.py†L287-L500】
  - **Type:** Optional `int` snowflake (`None` when unset or invalid). 【F:shared/config.py†L464-L501】
  - **Consumption sites:** Promo watcher setup gate and legacy env reporting surfaces. 【F:modules/onboarding/watcher_promo.py†L147-L169】【F:packages/c1c-coreops/src/c1c_coreops/cog.py†L3584-L3618】

- **WELCOME_CHANNEL_ID**
  - **Source:** Environment variable handled identically to promo via `_optional_id("WELCOME_CHANNEL_ID")`. 【F:shared/config.py†L287-L497】
  - **Type:** Optional `int` snowflake.
  - **Consumption sites:** Welcome watcher setup plus config surfaces. 【F:modules/onboarding/watcher_welcome.py†L152-L175】【F:packages/c1c-coreops/src/c1c_coreops/cog.py†L3584-L3618】

- **ADMIN_ROLE_IDS**
  - **Source:** Environment variable parsed into an `int` set through `_role_set("ADMIN_ROLE_IDS")`. 【F:shared/config.py†L532-L549】
  - **Type:** `set[int]` (empty when unset).
  - **Consumption sites:** Feature-toggle admin alerts and RBAC helpers (`c1c_coreops.rbac`) rely on this to mention admins and evaluate elevated access. 【F:modules/common/feature_flags.py†L41-L70】【F:packages/c1c-coreops/src/c1c_coreops/rbac.py†L1-L118】

- **RECRUITER_ROLE_IDS**
  - **Source:** Environment variable parsed with `_role_set("RECRUITER_ROLE_IDS")`. 【F:shared/config.py†L532-L557】
  - **Type:** `set[int]`.
  - **Consumption sites:** RBAC helper `is_recruiter` and reporting module role mentions for daily recruiter updates. 【F:packages/c1c-coreops/src/c1c_coreops/rbac.py†L1-L118】【F:modules/recruitment/reporting/daily_recruiter_update.py†L11-L76】

## 3) Import hygiene
- `modules/onboarding/watcher_welcome.py` — **OK** (defines helpers/Cog/async setup only; no work runs on import). 【F:modules/onboarding/watcher_welcome.py†L1-L175】
- `modules/onboarding/watcher_promo.py` — **OK** (mirrors welcome watcher; import only binds definitions). 【F:modules/onboarding/watcher_promo.py†L1-L170】

## 4) Event hooks we will extend
- `modules/onboarding/watcher_welcome.py::WelcomeWatcher.on_thread_update(before, after)` — listens for welcome threads transitioning to archived/locked, then logs closure to Sheets and runtime channel. 【F:modules/onboarding/watcher_welcome.py†L109-L141】
- `modules/onboarding/watcher_welcome.py::_ThreadClosureWatcher._record_closure(thread)` — shared logging pipeline that appends rows and emits log messages; candidate to append dialog triggers once closure detected. 【F:modules/onboarding/watcher_welcome.py†L117-L143】
- `modules/onboarding/watcher_promo.py::PromoWatcher.on_thread_update(before, after)` — identical pattern for promo threads; extend for promo dialog fallback. 【F:modules/onboarding/watcher_promo.py†L104-L137】
- `modules/onboarding/watcher_promo.py::_ThreadClosureWatcher._record_closure(thread)` — parallel logging routine for promo closures; same insertion point for 🧭 dialog orchestration. 【F:modules/onboarding/watcher_promo.py†L112-L137】
- `modules/onboarding/watcher_*::_announce(bot, message)` — helper creating background tasks that post watcher state to the runtime log channel via `rt.send_log_message`; reuse for dialog state notifications. 【F:modules/onboarding/watcher_welcome.py†L61-L68】【F:modules/onboarding/watcher_promo.py†L61-L68】

## 5) Gaps & decisions (to inform PR #2–#4)
- Welcome/promo watchers never check `welcome_dialog`, so the dialog feature cannot be toggled on/off yet — **Recommendation:** gate new dialog+fallback logic in `modules/onboarding/watcher_welcome.setup` / `watcher_promo.setup` by adding `feature_flags.is_enabled("welcome_dialog")` alongside existing toggles. 【F:modules/onboarding/watcher_welcome.py†L152-L175】【F:modules/common/feature_flags.py†L256-L266】
- No onboarding module handles 🧭 reactions; manual fallback path is entirely missing — **Recommendation:** introduce a new Cog (e.g., `modules/onboarding/reaction_fallback.py`) that subscribes to `on_raw_reaction_add` and reuses RBAC helpers for role checks before launching dialogs. (No existing code to cite — new file to add.)
- Dialog launch plumbing is absent in closure flow (`_record_closure` only writes Sheets/logs) — **Recommendation:** extend `_ThreadClosureWatcher._record_closure` to enqueue dialog tasks after logging when `welcome_dialog` is enabled and the Ticket Tool closure message is detected. 【F:modules/onboarding/watcher_welcome.py†L117-L143】
- Centralized channel scope helper for welcome vs. promo threads does not exist (each watcher compares parent IDs manually) — **Recommendation:** add a small shared helper (e.g., `modules/onboarding/thread_scopes.py::is_welcome_parent(thread)`) consuming `get_welcome_channel_id` / `get_promo_channel_id` to keep future reaction handlers consistent. 【F:modules/onboarding/watcher_welcome.py†L109-L168】【F:modules/onboarding/watcher_promo.py†L104-L163】

## 6) Risks & mitigations
- **Risk:** Feature toggle loader fails closed when Sheets access breaks, disabling watchers silently — **Mitigation:** ensure Phase 7 PR preloads `feature_flags.refresh()` during startup and surfaces failure via runtime logs before enabling dialog features. 【F:modules/common/feature_flags.py†L128-L266】
- **Risk:** `_record_closure` resets worksheet handle on any exception, potentially dropping dialog triggers if Sheets hiccup — **Mitigation:** wrap new dialog launch steps so they run before resetting state or retry after `self._worksheet` invalidation. 【F:modules/onboarding/watcher_welcome.py†L117-L143】
- **Risk:** Manual 🧭 fallback without consistent RBAC checks could bypass recruiter/admin intent — **Mitigation:** reuse `c1c_coreops.rbac` helpers to validate member roles before launching dialogs. 【F:packages/c1c-coreops/src/c1c_coreops/rbac.py†L1-L118】

## 7) Next PR outline (brief)
1. Wire `welcome_dialog` gating + dialog task placeholder inside `modules/onboarding/watcher_welcome.py` and `modules/onboarding/watcher_promo.py` setup/closure paths (no manual fallback yet).
2. Add shared onboarding helpers (`thread_scopes`, dialog launcher service) and unit coverage for channel/role validation touching `shared/config` + `c1c_coreops.rbac` consumers.
3. Implement 🧭 reaction fallback Cog (`modules/onboarding/reaction_fallback.py`) that invokes the dialog launcher, using role gating from `c1c_coreops.rbac` and logging through `modules.common.runtime`.

Doc last updated: 2025-10-28 (v0.9.7)
