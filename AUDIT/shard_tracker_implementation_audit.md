# Shard Tracker Implementation Audit

## Scope and method
- Audit target: `modules/community/shard_tracker` feature and all production-code integrations that invoke or depend on it.
- Sources reviewed: shard tracker module files (`cog.py`, `data.py`, `views.py`, `threads.py`, `mercy.py`, module `__init__`), shared config/runtime loading paths, and one operational status integration (`c1c-coreops`).
- Test-only code was excluded from behavior claims unless it reveals production wiring conflicts (none required here).

---

## 1) Feature purpose

### User-facing purpose
- The shard tracker gives each member a personal tracking panel for shard stash and mercy progression.
- The user opens `!shards` and gets a tabbed UI for Ancient, Void, Sacred, and Primal shards.
- It tracks:
  - stash owned,
  - pulls-since-legendary mercy counters,
  - primal pulls-since-mythical,
  - timestamps/depth of last notable pulls.

### Problem solved in-bot
- Centralizes shard/mercy self-tracking in one controlled channel and per-user private thread.
- Prevents channel clutter by routing panel output into private threads.
- Avoids manual spreadsheet edits by writing to a structured Milestones sheet row per Discord user.

---

## 2) Entry points

## 2.1 Prefix commands

### `!shards [type]` (group root)
- Defined in `ShardTracker.shards`.
- Trigger: prefix command `!shards` with optional `type` token.
- Who can use it: decorated with `@tier("user")`.
- Preconditions:
  1. Feature toggle must be enabled (`shared_config.features.shard_tracker_enabled` OR feature_flags key `shardtracker`/`shard_tracker`).
  2. Command must be in configured shard channel, or inside a thread under that channel.
- Flow:
  1. Resolves/creates private thread via `ShardThreadRouter.ensure_thread` when invoked in parent channel.
  2. Loads shard config from milestones/env via `ShardSheetStore.get_config`.
  3. Loads or lazily creates user row via `ShardSheetStore.load_record`.
  4. Saves snapshot username + `last_updated_iso`.
  5. Builds embed + interactive view for selected tab.
  6. Sends panel in user thread; replies in parent channel with pointer message.

### `!shards set <type> <count>`
- Defined as subcommand `ShardTracker.shards_set`.
- Trigger: `!shards set`.
- Who can use it: `@tier("user")`.
- Flow:
  1. Feature toggle check.
  2. Type resolution (`ancient|void|sacred|primal` with aliases).
  3. Count clamped to non-negative.
  4. Loads config + record, updates stash field, saves row.
  5. Replies with updated stash and logs action via runtime log channel.

## 2.2 Button/View/Modal entry points

### Shard panel buttons (`ShardTrackerView` + `_ShardButton`)
- Trigger: Discord component interactions from panel message.
- Ownership gate: only `owner_id` can use buttons; others get ephemeral denial.
- Button categories:
  - Tab selectors: `tab:overview|ancient|void|sacred|primal|last_pulls`
  - Action buttons (on shard tabs):
    - `action:stash:<tab>`
    - `action:pulls:<tab>`
    - `action:legendary:<tab>`
    - `action:last_pulls:<tab>`
- Controller target: `ShardTracker.handle_button_interaction`.

### Modals
- `_StashModal` → `process_stash_modal`
  - Numeric positive amount required.
  - Increases stash only.
- `_PullsModal` → `process_pulls_modal`
  - Numeric positive amount required.
  - Decreases stash (floor 0), increments mercy; primal also increments mythical mercy.
- `_LegendaryModal` → `process_legendary_modal`
  - Requires positive `total_pulls` and `0 <= after_champion <= total_pulls`.
  - Non-primal: resets legendary timestamp/depth and sets mercy to after value.
  - Primal: branches to ephemeral `_PrimalDropChoiceView` (Legendary or Mythical).
- `_LastPullsModal` → `process_last_pulls_modal`
  - Allows direct non-negative mercy counter edits.
  - For primal includes both legendary and mythical fields.

### Primal drop choice view
- `_PrimalDropChoiceView` is ephemeral and owner-gated.
- Buttons:
  - `Legendary` → `process_primal_choice(choice="legendary")`
  - `Mythical` → `process_primal_choice(choice="mythical")`
- Updates underlying record and refreshes panel message.

## 2.3 Startup/load entry points
- `modules/community/shard_tracker/__init__.py::setup` adds cog.
- `modules/common/runtime.py` loads all `COMMUNITY_EXTENSIONS`, including `modules.community.shard_tracker`.

## 2.4 External integration entry point (read-only status)
- `packages/c1c-coreops/.../cog.py` health embed imports `ShardSheetStore` and runs `get_config` to report shard tracker status/channel.


## 2.5 Function-level trace mapping

### `!shards` flow
- Entry: `!shards [type]`
  -> `ShardTracker.shards`
  -> `ShardTracker._ensure_feature_enabled`
  -> `ShardTracker._handle_shards`
  -> `ShardTracker._resolve_thread`
  -> (`ShardThreadRouter.ensure_thread` when invoked in parent channel)
  -> `ShardSheetStore.get_config`
  -> `ShardSheetStore.load_record`
  -> `ShardRecord.snapshot_name`
  -> `ShardSheetStore.save_record`
  -> `ShardTracker._build_panel`
  -> `ShardTracker._send_thread_message`
  -> `thread.send` (panel) + optional `ctx.reply` pointer in parent channel.

### `!shards set` flow
- Entry: `!shards set <type> <count>`
  -> `ShardTracker.shards_set`
  -> `ShardTracker._ensure_feature_enabled`
  -> `ShardTracker._handle_stash_set`
  -> `ShardTracker._resolve_kind` / `_resolve_kind_key`
  -> `ShardSheetStore.get_config`
  -> `ShardSheetStore.load_record`
  -> `setattr(record, kind.stash_field, count)`
  -> `ShardRecord.snapshot_name`
  -> `ShardSheetStore.save_record`
  -> `ctx.reply` + `ShardTracker._log_action`.

### Stash modal flow
- Entry: `action:stash:<tab>` button
  -> `_ShardButton.callback`
  -> `ShardTracker.handle_button_interaction`
  -> `interaction.response.send_modal(_StashModal)`
  -> `_StashModal.on_submit`
  -> `ShardTracker.process_stash_modal`
  -> `ShardTracker._apply_stash_increase`
  -> `ShardRecord.snapshot_name`
  -> `ShardSheetStore.save_record`
  -> `interaction.response.edit_message` + `ShardTracker._log_action`.

### Pulls modal flow
- Entry: `action:pulls:<tab>` button
  -> `_ShardButton.callback`
  -> `ShardTracker.handle_button_interaction`
  -> `interaction.response.send_modal(_PullsModal)`
  -> `_PullsModal.on_submit`
  -> `ShardTracker.process_pulls_modal`
  -> `ShardTracker._apply_pull_usage`
  -> `ShardRecord.snapshot_name`
  -> `ShardSheetStore.save_record`
  -> `interaction.response.edit_message` + `ShardTracker._log_action`.

### Legendary modal flow (non-primal)
- Entry: `action:legendary:<tab>` button where tab is `ancient|void|sacred`
  -> `_ShardButton.callback`
  -> `ShardTracker.handle_button_interaction`
  -> `interaction.response.send_modal(_LegendaryModal)`
  -> `_LegendaryModal.on_submit`
  -> `ShardTracker.process_legendary_modal`
  -> `ShardTracker._apply_pull_usage`
  -> `setattr(record, kind.mercy_field, drop_depth)`
  -> `ShardTracker._apply_legendary_reset`
  -> `setattr(record, kind.mercy_field, after_champion)`
  -> `ShardRecord.snapshot_name`
  -> `ShardSheetStore.save_record`
  -> `interaction.response.edit_message` + `ShardTracker._log_action`.

### Legendary modal flow (primal branch)
- Entry: `action:legendary:primal`
  -> `_ShardButton.callback`
  -> `ShardTracker.handle_button_interaction`
  -> `interaction.response.send_modal(_LegendaryModal)`
  -> `_LegendaryModal.on_submit`
  -> `ShardTracker.process_legendary_modal`
  -> `ShardTracker._apply_pull_usage`
  -> `setattr(record, "primals_since_lego", drop_depth)`
  -> `ShardRecord.snapshot_name`
  -> `ShardSheetStore.save_record`
  -> `interaction.response.send_message(..., view=_PrimalDropChoiceView, ephemeral=True)`
  -> user selects `Legendary` or `Mythical` button
  -> `_PrimalDropChoiceView.handle_legendary|handle_mythical`
  -> `ShardTracker.process_primal_choice`
  -> (`_apply_primal_legendary` or `_apply_primal_mythical`) + direct counter assignments
  -> `ShardRecord.snapshot_name`
  -> `ShardSheetStore.save_record`
  -> `target_message.edit` (panel refresh) + `interaction.response.edit_message("Logged!", view=None)` + `ShardTracker._log_action`.
- Persistence semantics: this is a **two-stage persisted workflow** (pre-choice save, then post-choice save), not one atomic write.

### Last Pulls / Mercy modal flow
- Entry: `action:last_pulls:<tab>` button
  -> `_ShardButton.callback`
  -> `ShardTracker.handle_button_interaction`
  -> `interaction.response.send_modal(_LastPullsModal)`
  -> `_LastPullsModal.on_submit`
  -> `ShardTracker.process_last_pulls_modal`
  -> `ShardTracker._apply_manual_mercy`
  -> `ShardRecord.snapshot_name`
  -> `ShardSheetStore.save_record`
  -> `interaction.response.edit_message` + `ShardTracker._log_action`.

---

## 3) Code map

### Primary module files
- `modules/community/shard_tracker/cog.py`
  - Main orchestration: commands, interaction dispatcher, modal processors, mutation logic, panel rendering, logging/alerts, channel/thread gates.
- `modules/community/shard_tracker/data.py`
  - Sheet config resolution and row persistence (`ShardSheetStore`).
  - Schema contract (`EXPECTED_HEADERS`) and row model (`ShardRecord`).
- `modules/community/shard_tracker/views.py`
  - Embed builders and Discord `View`/button composition.
- `modules/community/shard_tracker/threads.py`
  - Per-user private thread resolution/creation and ownership inference from thread name suffix `[user_id]`.
- `modules/community/shard_tracker/mercy.py`
  - Mercy rate math and chance formatting.
- `modules/community/shard_tracker/__init__.py`
  - Extension setup function.

### Supporting shared files
- `modules/community/__init__.py`
  - Extension registry includes shard tracker.
- `modules/common/runtime.py`
  - Loads community extensions on startup.
- `shared/config.py`
  - Merges Milestones Config tab into runtime config and exposes shard config getters.
- `packages/c1c-coreops/src/c1c_coreops/cog.py`
  - Operational health check for shard tracker config availability.

### Import/control flow summary
1. Runtime loads `modules.community.shard_tracker` extension.
2. `setup()` registers `ShardTracker` cog.
3. Command/button interaction lands in `ShardTracker`.
4. `ShardTracker` calls `ShardSheetStore` for config + record.
5. UI is built with `views.py` using display objects from record + mercy math.
6. Mutations call `ShardSheetStore.save_record`, then message edited/reposted.
7. Actions/errors are logged via `runtime.send_log_message`.

---

## 4) Data model and storage

## 4.1 Storage location
- Google Sheet: `MILESTONES_SHEET_ID` workbook.
- Worksheet tab: runtime key `SHARD_MERCY_TAB` (from milestones Config tab via shared config).
- Channel routing config: `SHARD_MERCY_CHANNEL_ID` (env override, else milestones Config tab value).

## 4.2 Row schema
`ShardSheetStore` requires exact header equality with `EXPECTED_HEADERS` (normalized to lowercase/trimmed):
- identity: `discord_id`, `username_snapshot`
- stash: `ancients_owned`, `voids_owned`, `sacreds_owned`, `primals_owned`
- mercy counters: `ancients_since_lego`, `voids_since_lego`, `sacreds_since_lego`, `primals_since_lego`, `primals_since_mythic`
- timestamps: `last_*_lego_iso`, `last_primal_mythic_iso`
- depths: `last_*_lego_depth`, `last_primal_mythic_depth`
- audit: `last_updated_iso`

## 4.3 Record lifecycle
- Read path:
  - Fetches full matrix (`afetch_values`), validates header row exactly.
  - Finds row where `discord_id` equals caller user id as string.
- Create path:
  - If missing user row, appends default row with zeros/empties and username snapshot.
- Duplicate row behavior:
  - If multiple rows share the same `discord_id`, `load_record` uses the **first matching row encountered** during top-down scan and stops.
  - No duplicate detection, warning, or merge behavior is implemented in the store layer.
- Update path:
  - Overwrites `A..V` for record row via worksheet update.
  - `last_updated_iso` refreshed on save.
- Validation behavior:
  - Invalid ints parse as `0`.
  - All persisted numeric fields are clamped non-negative when serialized.

---

## 5) Config dependencies

### Direct runtime config / env / sheet dependencies
- `MILESTONES_SHEET_ID` (required)
- `MILESTONES_CONFIG_TAB` (optional; defaults to `Config` for config-row ingestion)
- `SHARD_MERCY_TAB` (required config key in milestones config data)
- `SHARD_MERCY_CHANNEL_ID` (required resolved value; env can override sheet)
- Feature flags keys checked:
  - `shardtracker`
  - `shard_tracker`
  - plus snapshot key candidate `shard_tracker_enabled`
- Emoji configuration:
  - `SHARD_PANEL_OVERVIEW_EMOJI`
  - `SHARD_EMOJI_ANCIENT`
  - `SHARD_EMOJI_VOID`
  - `SHARD_EMOJI_SACRED`
  - `SHARD_EMOJI_PRIMAL`

### Role/channel dependencies
- Admin role IDs from `get_admin_role_ids()` are used only for error-mention notifications.
- Feature channel is strictly gated by resolved `SHARD_MERCY_CHANNEL_ID`.

### Hard-coded vs config-driven observations
- Config-driven:
  - Sheet id, tab name, channel id, emoji tags all configurable.
- Hard-coded (implementation constants):
  - Sheet column schema (`EXPECTED_HEADERS`) is hard-coded and must match exactly.
  - Mercy thresholds/increments/base rates are hard-coded in `MERCY_CONFIGS`.
  - Shard kinds and alias map are hard-coded.
  - Thread naming format (`Shards – <Display> [id]`) is hard-coded.

---

## 6) User flow

### Normal member success path
1. User runs `!shards` in shard parent channel.
2. Bot validates feature enabled + configured channel.
3. Bot creates/fetches user private thread.
4. Bot loads/creates user sheet row.
5. Bot posts panel in thread and pointer reply in parent channel.
6. User uses tabs/buttons/modals to log stash/pulls/champions.
7. Each accepted action updates sheet row and refreshes panel.

### Update/edit path
- `!shards set` force-sets stash count.
- Button-driven modal updates support:
  - incremental stash changes,
  - pull logging,
  - champion logging with after-pull depth,
  - manual mercy edits via “Last Pulls / Mercy”.

### Error/edge paths
- Wrong channel/thread parent: user gets “only available in <#channel>”.
- Someone else’s thread: blocked with “Please use your own shard thread…”.
- Disabled feature: user gets disabled message.
- Bad shard type: explicit error with valid options.
- Misconfigured sheet/tab/channel/schema: user sees admin-contact message; admin role ping log emitted.

---

## 7) Discord UI behavior

### Embeds
- Three embed modes:
  - Overview (all shard summaries + primal split)
  - Detail (single shard + progress bar; primal includes mythic block)
  - Last Pulls / Mercy info (timestamps/depth + static mercy rules + base rates)
- Footer is constant: `For info about how this works type !help shards`.
- Author icon uses configured emoji tag -> padded emoji URL pipeline.

### Components
- Buttons only (no select menus/reactions).
- Tabs always present; action row only on shard tabs.
- Primal legendary workflow adds temporary ephemeral choice view.

### Modal behavior
- All modal submissions enforce owner check.
- Numeric parsing failures produce ephemeral error replies.
- Successful modal updates edit the existing panel message.

### Visibility behavior
- Thread panel messages are regular thread messages (public to thread participants).
- Many failures are ephemeral for interaction UX.
- Primal Legendary/Mythical disambiguation prompt is ephemeral.

### Permission/gating behavior
- `@tier("user")` command access tier.
- Ownership gate at button and modal layers.
- Channel gate by configured parent channel id.

---

## 8) Background behavior

### Scheduled/recurring jobs
- No `tasks.loop`, scheduler, or periodic refresh logic in shard tracker module.

### Startup hooks
- Loaded at bot startup as community extension.
- No shard-specific startup migration/backfill task.

### Restart behavior
- In-memory caches reset:
  - `ShardSheetStore` config cache (TTL 300s)
  - thread owner/user-thread maps in `ShardThreadRouter`
  - per-user locks in cog
- Persistent state remains in Google Sheet.
- Existing threads are rediscovered by parsing thread names or cache repopulation when used.

---

## 9) Validation and rules

### Business logic currently implemented
- Counts are clamped non-negative.
- Stash decrement cannot go below zero.
- Mercy counters increment on pulls.
- Primal pulls increment both legendary and mythical mercy counters.
- Legendary logging requires `after_champion <= total_pulls`.
- Non-primal legendary logging records timestamp/depth reset behavior.
- Primal legendary/mythical branch has separate counter update logic.

### Mercy math
- Computed in `mercy_state` from hard-coded configs:
  - Ancient/Void: base 0.5%, threshold 200, +5% per overflow pull.
  - Sacred: base 6%, threshold 12, +2% per overflow pull.
  - Primal Legendary: base 1%, threshold 75, +1% per overflow pull.
  - Primal Mythical: base 0.5%, threshold 200, +10% per overflow pull.
- Chance capped at 100%.
- UI progress bar:
  - before threshold: green/white fill
  - overflow: orange/black fill over fixed overflow range of 100 pulls.

### Duplicate prevention / concurrency
- Per-user async lock serializes updates across commands/interactions.
- Sheet write operations are lock-protected in store (`_sheet_lock`).

---

## 10) Error handling

### Explicit exception handling
- Config and schema exceptions:
  - `ShardTrackerConfigError`
  - `ShardTrackerSheetError`
- User gets friendly failure text; admins notified via log channel mention.

### User-visible failures
- Wrong channel/thread, unknown action/type, invalid numeric input, invalid logical values (e.g., after>total).
- Interaction errors are mostly ephemeral.

### Logging
- One-time config snapshot log in `data.py` (`_log_config_snapshot`).
- Action logging in cog (`log.info("shard action", extra=...)`).
- Runtime ops channel messages for actions and critical errors.

### Potential silent/soft-fail areas
- Thread active-thread fetch exceptions are swallowed in router and treated as no fetched threads.
- Feature flag lookup exceptions are logged and fallback to other keys/False.

---

## 11) Risks / weak spots

1. **Strict schema coupling risk**
   - Header mismatch to `EXPECTED_HEADERS` hard-fails the feature.
2. **Possible append race edge**
   - New-row creation uses fetch + append; per-user lock reduces collisions for same user but does not globally dedupe concurrent first writes across process instances.
3. **Thread ownership inferred from name suffix**
   - If thread name format changes manually, owner parsing may fail or permit ambiguity.
4. **Hard-coded mercy constants**
   - Thresholds/rates are code constants, not sheet-configurable.
5. **Operational metadata drift**
   - `docs/_meta/COMMAND_METADATA.md` lists `mercy`, `lego`, `mythic primal`, but those commands are not currently registered in cog.
6. **Env/config split for channel id**
   - Channel id precedence is env over sheet; behavior is correct but could confuse operators if both are set inconsistently.

---

## 12) Architecture fit assessment

### Pattern alignment
- Follows cog-based modular pattern.
- Uses shared config facade and runtime logging conventions.
- Uses extension registry + startup loader pattern.
- Uses sheet-backed storage with dedicated data access layer.

### Explicit checks requested
- Hard-coded tab names?
  - Tracker data tab is config-driven (`SHARD_MERCY_TAB`) ✅
  - Milestones config tab defaults to hard-coded `Config` when env not set.
- Hard-coded column names?
  - Yes: full hard-coded `EXPECTED_HEADERS` list.
- Hard-coded schema assumptions?
  - Yes: exact normalized header equality check.
- Logic that should be config-driven?
  - Mercy rates/thresholds and type alias set are hard-coded in code.

### Notable architecture inconsistency
- Command metadata doc lists shard-related commands not present in current cog implementation (stale documentation metadata).

---

## 13) Summary

### How this feature currently works
- Shard tracker is a user-tier prefix-command feature (`!shards`, `!shards set`) that is channel-gated to a configured Shards & Mercy channel.
- It creates/uses per-user private threads, loads/creates a per-user sheet record, and presents an owner-locked button/modal UI.
- All user actions mutate a single structured row in the milestones shard worksheet and refresh the panel embed immediately.
- Mercy/chance calculations are deterministic and code-defined, including primal legendary + mythical split behavior.

### What needs care before extending it
- Preserve sheet header contract or add migration/compat handling before schema changes.
- Maintain consistency across feature flags, sheet config, and env overrides for channel routing.
- Be careful with primal mutation logic (two-counter interactions).
- Update metadata/docs in lockstep with actual registered commands to avoid operator confusion.
- If adding new shard types or pity rules, multiple hard-coded maps/constants must be updated together (`SHARD_KINDS`, `MERCY_CONFIGS`, base rates, UI labels/buttons, schema fields).

---

## 14) All Data Mutation Points

This section inventories every production mutation site for shard tracker record state.

| Function | Mutated fields | Mutation type | Save path |
|---|---|---|---|
| `ShardTracker._handle_stash_set` | one of `ancients_owned` / `voids_owned` / `sacreds_owned` / `primals_owned`; `username_snapshot`; `last_updated_iso` | **overwrite** stash to provided count (clamped non-negative), snapshot refresh | direct `ShardSheetStore.save_record` |
| `ShardTracker.process_stash_modal` via `_apply_stash_increase` | selected `*_owned`; `username_snapshot`; `last_updated_iso` | **increment** stash by positive amount | direct `save_record` |
| `ShardTracker.process_pulls_modal` via `_apply_pull_usage` | selected `*_owned` (decrement floor 0), selected `*_since_lego` (increment), plus `primals_since_mythic` for primal; `username_snapshot`; `last_updated_iso` | **increment/decrement** | direct `save_record` |
| `ShardTracker.process_legendary_modal` (non-primal) | selected `*_owned` (decrement), selected `*_since_lego`, selected `last_*_lego_depth`, selected `last_*_lego_iso`, then selected `*_since_lego` set to `after_champion`; `username_snapshot`; `last_updated_iso` | mixed: **increment/decrement + reset + overwrite** | direct `save_record` |
| `ShardTracker.process_legendary_modal` (primal pre-choice stage) | `primals_owned`, `primals_since_lego`, `primals_since_mythic`, `username_snapshot`, `last_updated_iso` | **increment/decrement + overwrite** before choice prompt | direct `save_record` |
| `ShardTracker.process_primal_choice` choice=`legendary` | `primals_since_lego`, `last_primal_lego_depth`, `last_primal_lego_iso`, `primals_since_mythic`, `username_snapshot`, `last_updated_iso` | mixed: **reset + overwrite + increment** (stage 2 of primal legendary workflow) | direct `save_record` |
| `ShardTracker.process_primal_choice` choice=`mythical` | `last_primal_mythic_depth`, `last_primal_mythic_iso`, `primals_since_mythic`, `primals_since_lego`, `username_snapshot`, `last_updated_iso` | mixed: **record event + overwrite + increment** (stage 2 of primal legendary workflow) | direct `save_record` |
| `ShardTracker.process_last_pulls_modal` via `_apply_manual_mercy` | non-primal: one `*_since_lego`; primal: `primals_since_lego` and `primals_since_mythic`; `username_snapshot`; `last_updated_iso` | **overwrite** counters | direct `save_record` |
| `ShardTracker._handle_shards` | `username_snapshot`, `last_updated_iso` | **overwrite/refresh metadata only** | direct `save_record` |
| `ShardSheetStore.load_record` when row missing -> `_append_row` | full row initialized via `_new_record` defaults | **create/append** new user row | indirect persistence via `_append_row` |

### Field-level “where can this value change?” quick map
- `*_owned` fields: `_handle_stash_set`, `_apply_stash_increase`, `_apply_pull_usage`.
- non-primal `*_since_lego`: `_apply_pull_usage`, `_apply_legendary_reset`+follow-up overwrite in `process_legendary_modal`, `_apply_manual_mercy`.
- `primals_since_lego`: `_apply_pull_usage`, primal branch in `process_legendary_modal`, `_apply_primal_legendary`, `process_primal_choice`, `_apply_manual_mercy`.
- `primals_since_mythic`: `_apply_pull_usage` (primal only), `process_primal_choice`, `_apply_manual_mercy`.
- `last_*_lego_iso` / `last_*_lego_depth`: `_apply_legendary_reset` (non-primal); primal legendary timestamp/depth via `_apply_primal_legendary`.
- `last_primal_mythic_iso` / `last_primal_mythic_depth`: `_apply_primal_mythical`.
- `last_updated_iso`: always set by `ShardSheetStore.save_record`; initialized on new row.
- Primal legendary logging note: persistence is explicitly two-step (`process_legendary_modal` pre-choice save, then `process_primal_choice` post-choice save), so intermediate state can exist briefly.

---

## 15) Concurrency model

### Locks and where applied
1. **Per-user lock in cog** (`ShardTracker._user_lock(user_id)`)
   - Applied in command and modal/primal handlers around load→mutate→save sequences.
   - Protects against concurrent updates for the same user **within one bot process**.
2. **Config lock in data store** (`ShardSheetStore._config_lock`)
   - Applied in `get_config`.
   - Protects config cache refresh consistency and avoids duplicate refresh work in-process.
3. **Sheet write lock in data store** (`ShardSheetStore._sheet_lock`)
   - Applied around `worksheet.update` and append path.
   - Serializes writes in-process to reduce concurrent write overlap.

### What the locks protect
- In-process same-user mutation serialization for command/button/modal collisions.
- In-process write ordering for calls that reach `save_record`/`_append_row`.
- In-process cache consistency for config reads.

### What the locks do NOT protect
- Cross-process/cross-instance concurrency (no distributed lock).
- Lost-update patterns between separate bot instances reading old row then writing new row.
- Global uniqueness of row append under multi-instance first-use race.
- Discord-level duplicate interaction delivery semantics outside handler logic.

---

## 16) Race condition risks

### Simultaneous modal submissions for same user (single process)
- Status: **mostly safe**.
- Why: all modal processors use per-user lock around read/mutate/save.
- Residual risk: ordering is serialized but final result depends on arrival order (expected behavior).

### Command + modal overlap for same user (single process)
- Status: **mostly safe**.
- Why: both command handlers and modal handlers use same per-user lock.
- Residual risk: user intent confusion if two operations are queued quickly and applied sequentially.

### Rapid updates across different shard types (same user)
- Status: **safe in-process, order-dependent outcomes**.
- Why: same user lock serializes all shard types for that user.

### Multi-user simultaneous updates
- Status: **safe enough by design** for independent user rows.
- Why: locks are per-user, allowing concurrency between users.
- Residual risk: sheet API rate/latency issues, not logical row collision in normal cases.

### Multi-instance bot scenario
- Status: **risky / undefined without external coordination**.
- Why:
  - user locks are process-local,
  - sheet write lock is process-local,
  - both instances can read stale row state and overwrite each other,
  - append-on-miss can duplicate user rows if two instances create simultaneously.

### Thread routing cache races
- Status: **generally safe in-process** due to router lock.
- Residual risk in multi-instance: caches are not shared, but name parsing fallback reduces impact.

---

## 17) UI → logic → data mapping

| UI Element | Handler | Processing Function | Data Mutation | Save Call |
|------------|--------|--------------------|--------------|----------|
| `+ Stash` button -> `_StashModal` submit | `_StashModal.on_submit` | `ShardTracker.process_stash_modal` -> `_apply_stash_increase` | selected `*_owned` increment | direct `ShardSheetStore.save_record` |
| `- Pulls` button -> `_PullsModal` submit | `_PullsModal.on_submit` | `ShardTracker.process_pulls_modal` -> `_apply_pull_usage` | stash decrement + mercy increment (plus primal mythic increment) | direct `save_record` |
| `Got Legendary` / `Got Legendary/Mythical` -> `_LegendaryModal` submit | `_LegendaryModal.on_submit` | `ShardTracker.process_legendary_modal` | non-primal: pull usage + reset/overwrite legendary mercy + timestamp/depth; primal: pre-choice counter updates | direct `save_record` (always; primal then awaits second mutation step) |
| `Last Pulls / Mercy` -> `_LastPullsModal` submit | `_LastPullsModal.on_submit` | `ShardTracker.process_last_pulls_modal` -> `_apply_manual_mercy` | overwrite mercy counters (legendary; plus mythic for primal) | direct `save_record` |
| Primal choice `Legendary` button | `_PrimalDropChoiceView.handle_legendary` | `ShardTracker.process_primal_choice(choice="legendary")` | reset/overwrite primal legendary; increment/overwrite mythic path counters | direct `save_record` |
| Primal choice `Mythical` button | `_PrimalDropChoiceView.handle_mythical` | `ShardTracker.process_primal_choice(choice="mythical")` | record mythical timestamp/depth; overwrite mythic mercy; increment legendary mercy path | direct `save_record` |

---

## 18) Change Impact Map

### A) If a new shard type is added
Must be updated together:
- `SHARD_KINDS` mapping and `ShardKind` field bindings.
- `_TYPE_ALIASES` resolution map.
- `MERCY_CONFIGS` with new thresholds/increments/base rates.
- `_BASE_RATES` display map in cog.
- Sheet schema (`EXPECTED_HEADERS`) + `ShardRecord` fields + row parse/serialize logic.
- UI tab labels/buttons (`TAB_LABELS`, tab button list in `ShardTrackerView`, author naming/colors).
- Embed display builders and any primal-special-case logic generalization.
- Validation and kind resolution paths (`_resolve_kind`, `_invalid_type_message`).

### B) If mercy rules change
Must be updated together:
- `MERCY_CONFIGS` constants.
- `mercy_state` behavior (if formula semantics change beyond constants).
- UI text that describes pity rules (`build_last_pulls_embed` static info lines).
- Optional base-rate display map `_BASE_RATES` to keep shown rates aligned.

### C) If sheet schema changes
Must be updated together:
- `EXPECTED_HEADERS` exact list/order.
- `ShardRecord` dataclass fields.
- `ShardRecord.to_row` mapping.
- `_row_to_record` parser field extraction.
- `save_record` write range assumptions (`A..V`) if column count changes.
- Any docs/ops references that encode expected headers.

---

## 19) Violations of project architecture rules

### 1) Hard-coded schema (`EXPECTED_HEADERS`)
- Rule fit: conflicts with the project guidance of minimizing hard-coded sheet assumptions.
- Risk introduced: any column rename/reorder breaks runtime with full feature outage.
- To become config-driven: add schema indirection/column mapping from config tab (or strongly versioned schema contract with migration handling).

### 2) Hard-coded shard types (`SHARD_KINDS`, `TAB_LABELS`, aliases)
- Rule fit: partially violates config-driven extensibility for feature data dimensions.
- Risk introduced: extending shard taxonomy requires broad code edits and raises drift risk across UI/data/math.
- To become config-driven: define shard type registry in sheet config and generate tab/actions/display bindings from that registry.

### 3) Hard-coded mercy configuration (`MERCY_CONFIGS`)
- Rule fit: hard-coded operational rules instead of config-served feature behavior.
- Risk introduced: rule updates require deployment and can drift from documented ops settings.
- To become config-driven: store thresholds/base/increment values in config tab and validate/parse into runtime mercy configs.

---

## 20) Failure Surface Map

| Failure point | Handling type | User-visible behavior | Logging behavior | Recovery behavior |
|---|---|---|---|---|
| Feature disabled (`_feature_enabled` false) | expected/caught | Reply/ephemeral “Shard & Mercy tracking is currently disabled...” | optional feature-flag exception logging in cog | user retries after toggles enabled |
| Config missing/invalid (`get_config`) | caught + admin alert | command: “Shard tracker is misconfigured...”; interaction: “Shard tracker misconfigured: ...” | admin ping via `runtime.send_log_message`; config snapshot log once | fix config then retry |
| Sheet header mismatch/empty (`load_record`) | caught + admin alert | user sees misconfigured sheet/admin-contact message | admin ping + error reason logged | fix headers then retry |
| Sheet read failure (`afetch_values` exception path surfaced as config/sheet failure handlers) | may bubble / generic Discord failure (unless raised as handled config/sheet error) | same misconfigured/error message path when mapped to handled error; otherwise generic interaction/command failure | error logged/forwarded through notify path when in handled path | transient retry by user after backend recovers |
| Sheet write failure (`save_record` update/append exceptions) | may bubble / generic Discord failure (except where explicitly caught) | interaction may fail with generic error surface from handler exception path; command flows report config failure only when captured | exception logs + possible admin notification where caught | retry after sheet/API recovery |
| Thread creation failure (`ensure_thread`/`create_thread`) | may bubble / generic Discord failure (or caught via `thread is None` guard) | “Unable to locate or create your shard thread.” when `thread is None` | no dedicated shard-specific error log in `_send_thread_message`; upstream exceptions depend on call site | manual rerun command; fix channel/thread perms |
| Wrong channel/thread ownership | expected/caught | explicit reply: only configured channel / use own thread | no admin alert (expected user error) | user moves to correct channel/thread |
| Invalid modal numeric input | expected/caught | ephemeral validation error | no admin logging | user corrects input and resubmits |
| Interaction on stale/expired ephemeral view (`_PrimalDropChoiceView` timeout) | may bubble / generic Discord failure | Discord interaction fails due expired view (client-side “interaction failed”) | no explicit timeout handler logging in view | rerun shard flow and log again |
| Non-owner button/modal interaction | expected/caught | ephemeral denial message | no admin logging | owner performs action |

---

## 21) Refactor Hazard Map

Risk ranking is based on mutation breadth, coupling, and persistence sensitivity in current code paths.

| Subsystem | Risk to modify | Why |
|---|---|---|
| Data/schema persistence (`ShardRecord`, `EXPECTED_HEADERS`, `load_record`/`save_record`) | **High** | Tightly coupled schema/order assumptions and direct row-range writes; errors surface as feature-wide failures. |
| Primal choice flow (`process_legendary_modal` + `_PrimalDropChoiceView` + `process_primal_choice`) | **High** | Two-stage persisted workflow with branching counter semantics and multiple coupled fields. |
| Thread routing (`ShardThreadRouter`, `_resolve_thread`) | **Medium** | User isolation depends on naming + parent channel checks; behavior is sensitive but localized. |
| UI/views (`ShardTrackerView`, embed builders, modal classes) | **Medium** | Broad UX surface but mostly deterministic transformations over record state. |
| Config loading (`ShardSheetStore.get_config`, `shared.config` merge path) | **Medium** | Multiple config sources and precedence rules; misconfiguration breaks feature availability. |
