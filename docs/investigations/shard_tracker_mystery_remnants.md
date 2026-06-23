# Shard Tracker Mystery + Remnants Investigation

## A. Current Architecture Summary

The shard tracker is implemented as a Discord cog owned by `ShardTracker`, with command handling, sheet persistence orchestration, panel building, modal processing, button routing, clan sharing, and reminder support centered in `modules/community/shard_tracker/cog.py`.

The current tracker supports exactly four shard types in its primary runtime model:

- Ancient
- Void
- Sacred
- Primal

Those four shard types are represented by `ShardKind` entries in `SHARD_KINDS`, not by an enum. Each `ShardKind` currently carries the storage field names needed for stash count, Legendary mercy count, last Legendary timestamp/depth, and a `MercyConfig`.

Primal has special handling beyond the normal `ShardKind` Legendary model. Primal Legendary mercy uses the `ShardKind` path, while Primal Mythical mercy is tracked separately through `primals_since_mythic`, `last_primal_mythic_iso`, and `last_primal_mythic_depth`. Primal result handling also uses a follow-up choice view so the user can record whether the pull was Legendary or Mythical.

Persistence is sheet-backed through `ShardSheetStore` in `modules/community/shard_tracker/data.py`. The main shard tracker worksheet schema is strict and order-sensitive: the normalized header row must match `EXPECTED_HEADERS` exactly.

Rendering is split between `ShardTracker._build_panel()` and embed builders in `modules/community/shard_tracker/views.py`. `_build_panel()` loads and converts record state into display objects, chooses overview/detail/last-pulls rendering, and constructs the view. `views.py` owns `build_overview_embed()`, `build_detail_embed()`, `build_last_pulls_embed()`, and `ShardTrackerView` button construction.

`ShardTrackerView` constructs the Discord tab and action buttons. Tabs are currently hardcoded, and action availability is based mostly on whether the active tab is a known shard tab, with a small Primal-specific label branch.

## B. Current Files, Functions, and Classes

### Files

- `modules/community/shard_tracker/__init__.py` — extension setup; adds the `ShardTracker` cog.
- `modules/community/shard_tracker/cog.py` — main cog, commands, modals, button handling, state mutation, share-to-clan, and reminder logic.
- `modules/community/shard_tracker/data.py` — sheet config, strict schema, record dataclass, load/save helpers, clan/reminder sheet helpers.
- `modules/community/shard_tracker/mercy.py` — mercy config objects, mercy chance math, and percent formatting.
- `modules/community/shard_tracker/views.py` — Discord views/buttons, display dataclasses, embed builders, and progress bars.
- `modules/community/shard_tracker/threads.py` — per-user shard tracker thread routing.
- `modules/community/shard_tracker/scheduler.py` — weekly shard reminder scheduler registration/runner.

### Discord command entry points

- `!shards [type]` opens the shard tracker panel.
- `!shards set <type> <count>` sets a stash count.
- `!shards reminder-debug [force|force-send]` runs reminder diagnostics.

The command that opens the shard tracker panel is the `shards` command group with `invoke_without_command=True`. The cog/class that owns the command is `ShardTracker`.

### Relevant classes and functions

- `ShardTracker`
- `ShardTrackerController`
- `ShardSheetStore`
- `ShardTrackerView`
- `_ShardButton`
- `_StashModal`
- `_PullsModal`
- `_LegendaryModal`
- `_LastPullsModal`
- `_PrimalDropChoiceView`
- `build_overview_embed()`
- `build_detail_embed()`
- `build_last_pulls_embed()`
- `mercy_state()`

### Rendering entry points

`ShardTracker._build_panel()` builds the display objects and calls `build_overview_embed()` for the overview tab, `build_detail_embed()` for individual shard tabs, and `build_last_pulls_embed()` for the last-pulls/mercy tab.

`build_overview_embed()` currently renders Ancient, Void, and Sacred in one generic non-Primal loop, then renders Primal separately because Primal has both Legendary and Mythical tracks.

`build_detail_embed()` always includes a Legendary progress section for the selected `ShardDisplay`. It includes a Primal Mythical section only when a `MythicDisplay` is passed, which currently happens only for the Primal tab.

## C. Current Sheet and Config Behavior

The main shard tracker tab is read from config key `shard_mercy_tab`, backed by the `SHARD_MERCY_TAB` config value. The sheet ID comes from `get_milestones_sheet_id()`.

The shard tracker channel ID is resolved from `SHARD_MERCY_CHANNEL_ID` in the environment first, then from `shard_mercy_channel_id` in sheet config. Missing required config raises `ShardTrackerConfigError`.

The main sheet headers are defined by `EXPECTED_HEADERS` in `modules/community/shard_tracker/data.py`. Header validation requires exact ordered equality, not merely “contains required headers.” Invalid or missing headers raise `ShardTrackerSheetError`.

Command and button flows convert sheet/config errors into user-facing misconfigured messages and send/admin-log notifications. Users see messages such as “Shard tracker is misconfigured. Please contact an admin.” or “Shard tracker sheet misconfigured. Please contact an admin.” depending on flow.

There is no separate flexible header mapping registry for the main shard tracker columns. The current pattern is:

1. Define `EXPECTED_HEADERS`.
2. Validate exact ordered equality.
3. Build a header map from the validated header row.
4. Parse/save known fields by hardcoded schema field names in `ShardRecord._row_to_record()` and `ShardRecord.to_row()`.

`save_record()` currently writes the fixed range `A{row}:V{row}`, matching the current 22-column schema.

### Exact current headers

The current expected main shard tracker headers are:

- `discord_id`
- `username_snapshot`
- `ancients_owned`
- `voids_owned`
- `sacreds_owned`
- `primals_owned`
- `ancients_since_lego`
- `voids_since_lego`
- `sacreds_since_lego`
- `primals_since_lego`
- `primals_since_mythic`
- `last_ancient_lego_iso`
- `last_void_lego_iso`
- `last_sacred_lego_iso`
- `last_primal_lego_iso`
- `last_primal_mythic_iso`
- `last_ancient_lego_depth`
- `last_void_lego_depth`
- `last_sacred_lego_depth`
- `last_primal_lego_depth`
- `last_primal_mythic_depth`
- `last_updated_iso`

This matches the current visible sheet shape from the investigation prompt exactly, including order.

### Defaults and fallback behavior

- Blank integer cells parse to `0`.
- Blank string cells parse to an empty string.
- New records default counts/counters to `0` and timestamps to an empty string.
- No alternate or fallback column names exist for the main tracker sheet.
- Clan/reminder tabs use a different pattern: they check for missing required headers by membership rather than exact ordered equality.

## D. Current Shard Type Assumptions

Mercy configurations are defined in `MERCY_CONFIGS` in `modules/community/shard_tracker/mercy.py`.

Shard tracker shard types are defined in `SHARD_KINDS` in `modules/community/shard_tracker/cog.py`.

Each `ShardKind` currently has these fields:

- `key`
- `label`
- `stash_field`
- `mercy_field`
- `mercy_config`
- `timestamp_field`
- `depth_field`

Ancient, Void, and Sacred use only the `ShardKind` Legendary model. Primal uses `ShardKind` for Legendary mercy and separate record fields/display handling for Mythical mercy.

The current model has no first-class support for:

- owned-count-only resources
- Mythical-only resources
- no-mercy resources

The absence of first-class support is structural: `ShardKind` requires Legendary mercy fields, `_build_display()` always builds a Legendary `MercySnapshot`, `build_detail_embed()` always adds a Legendary progress field, and `ShardTrackerView` currently grants pull/result/mercy controls to shard detail tabs based on tab membership rather than per-resource capabilities.

## E. Current Mercy Model

The current mercy formula is:

```python
steps = max(0, pulls - config.threshold)
chance = min(1.0, config.base_rate + steps * config.increment)
```

This means chance remains at the base rate through exactly the threshold count and begins increasing only when `pulls_since > threshold`.

Current mercy configs:

- Ancient Legendary: base `0.5%`, threshold `200`, increment `5%`.
- Void Legendary: base `0.5%`, threshold `200`, increment `5%`.
- Sacred Legendary: base `6%`, threshold `12`, increment `2%`.
- Primal Legendary: base `1%`, threshold `75`, increment `1%`.
- Primal Mythical: base `0.5%`, threshold `200`, increment `10%`.

Legendary display is built through `_build_display()`, which reads the `ShardKind.mercy_field`, calls `mercy_state(kind.key, since)`, and returns a `ShardDisplay`.

Primal Mythical display is built through `_build_mythic_display()`, which calls `mercy_state("primal_mythic", record.primals_since_mythic)`.

`_apply_pull_usage()` increments the Legendary mercy field for every current shard kind and increments `primals_since_mythic` only for Primal.

Remnant math can be represented by the existing `MercyConfig`/`mercy_state()` formula with:

- `base_rate=0.025`
- `threshold=24`
- `increment=0.01`

However, Remnants cannot fit the current UI/state model cleanly without targeted changes because the current shard model assumes Legendary mercy, while Remnants are intended to be Mythical-only.

## F. Current Button and View Model

`ShardTrackerView` currently hardcodes the tab order:

- `overview`
- `ancient`
- `void`
- `sacred`
- `primal`
- `last_pulls`

The overview tab currently has only one action button:

- Share to Clan

Detail tabs currently get:

- `+ Stash`
- `- Pulls`
- Share to Clan
- Got Legendary, or Got Legendary/Mythical for Primal
- Last Pulls / Mercy

Button custom IDs are parsed as:

- `tab:<tab>`
- `action:<action>:<tab>`

`_apply_pull_usage()` currently assumes one pull costs one owned shard/resource unit. It decreases owned count by the pull amount, increments the Legendary mercy counter by that same amount, and increments `primals_since_mythic` by that same amount only for Primal.

Remnants do not fit this unchanged if `remnants_owned` stores raw Cursed Remnant currency, because each Remnant Summon costs 100 Cursed Remnants rather than one owned unit.

Mystery would incorrectly get pull/result/mercy controls if it were simply added to `shard_labels` without capability-aware view logic, because the current view grants detail-tab actions by tab membership.

## G. Current Share to Clan Behavior

Share to Clan is implemented mainly in `_handle_share_summary_action()` and `_build_share_embed()` in `modules/community/shard_tracker/cog.py`.

The Share to Clan button is added by `ShardTrackerView._add_share_button()` in `modules/community/shard_tracker/views.py`.

Share to Clan exists on overview and detail pages. The overview gets Share to Clan as its only action, and detail tabs get Share to Clan as part of the primary detail action buttons.

Sharing rebuilds a fresh overview-style payload from `ShardRecord`; it does not share the currently visible embed directly. `_build_share_embed()` rebuilds displays from record state and renders an overview payload before changing title/description for clan sharing.

The share custom ID tab payload is ignored by the handler. Tests currently confirm that share routing does not pass the active tab through to `_handle_share_summary_action()`.

Current share tests expect overview fields for:

- Ancient
- Void
- Sacred
- Primal

Adding Mystery/Remnants later requires updating `_build_share_embed()`, `build_overview_embed()`, and tests so the shared overview includes the new resource types correctly.

## H. Existing Tests

Current shard tracker test files:

- `tests/community/shard_tracker/conftest.py`
- `tests/community/shard_tracker/test_cog.py`
- `tests/community/shard_tracker/test_data.py`
- `tests/community/shard_tracker/test_mercy.py`
- `tests/community/shard_tracker/test_reminders.py`
- `tests/community/shard_tracker/test_threads.py`

Current coverage includes:

- config env/sheet fallback
- missing tab config
- row parsing
- new row append
- invalid header failure
- mercy before/after threshold
- mercy cap
- percent formatting
- type aliases
- command registration
- Legendary reset depth
- Primal Mythical reset/counter behavior
- manual mercy setting
- Share to Clan success and missing destination
- overview/detail Share to Clan button layout
- share embed overview payload
- share routing ignores active tab
- reminder scheduling/dedupe
- shard thread reuse and owner parsing

The existing tests provide regression coverage for much of Ancient/Void/Sacred/Primal behavior, but additional tests would be needed for count-only Mystery behavior and Mythical-only Remnant behavior.

## I. Recommended Follow-up Implementation

This section documents a recommended future implementation plan only. It does not implement Mystery Shards, Remnants, sheet migrations, config keys, or Discord view/button changes.

### Recommended new columns

If Mystery is count-only and Cursed Remnants are raw currency with Mythical mercy based on summons/misses, the recommended new main shard tracker columns are:

- `mysteries_owned`
- `remnants_owned`
- `remnants_since_mythic`
- `last_remnant_mythic_iso`
- `last_remnant_mythic_depth`

No new sheet tab appears necessary.

### Required schema/storage changes later

A future implementation must update:

- `EXPECTED_HEADERS`
- `ShardRecord`
- `ShardRecord.to_row()`
- `_row_to_record()`
- `save_record()` range

If these five columns are appended to the existing 22-column schema, `save_record()` must be updated from `A:V` to `A:AA`.

The sheet must be migrated in lockstep with code because current validation requires exact ordered header equality. Deploying code before the sheet headers are updated, or updating sheet headers before compatible code is deployed, will cause the shard tracker to fail validation.

### Likely files to change later

- `modules/community/shard_tracker/data.py`
- `modules/community/shard_tracker/mercy.py`
- `modules/community/shard_tracker/cog.py`
- `modules/community/shard_tracker/views.py`
- `shared/config.py` only if adding emoji/config keys
- `docs/ops/modules/ShardTracker.md`

### Later tests to add/update

- storage/schema tests for new fields and save range
- Remnant mercy formula tests
- Mystery alias/stash tests
- Remnant alias/summon tests
- Mystery no-mercy/no-result-button tests
- Remnant Mythical reset tests
- overview rendering tests
- detail rendering tests
- Share to Clan tests
- existing Ancient/Void/Sacred/Primal regression tests

## J. Risks and Guardrails

### Risks

- Current schema is strict and order-sensitive.
- Existing shard tracker will fail if code and sheet headers are not migrated together.
- `save_record()` range must not remain `A:V` after adding columns.
- Mystery cannot be dropped into `SHARD_KINDS` unchanged because `ShardKind` assumes mercy fields.
- Remnants cannot reuse `- Pulls` unchanged if `remnants_owned` stores raw Cursed Remnant currency.
- Remnant mercy should count summons/misses, not raw currency.
- Current view hardcodes tabs.
- Current button actions are not capability-aware.
- Current detail rendering always assumes Legendary mercy.
- Current overview rendering special-cases Primal.
- Share to Clan rebuilds overview from record state and must be updated separately.
- Persistent old Discord panels may still have old button custom IDs.
- Avoid hidden fallbacks, duplicate config paths, and silent legacy behavior.

### Open confirmations before implementation

- Should `remnants_owned` store raw Cursed Remnant currency? Recommended: yes.
- Should `remnants_since_mythic` count Remnant Summons/misses? Recommended: yes.
- Should Mystery have only `+ Stash` and Share to Clan? Recommended: yes for count-only tracking.
- Should Remnant use a separate summon logging flow that deducts 100 per summon? Recommended: yes.
- Should Remnant Mythical appear on Last Pulls / Mercy? Needs product decision.
- Should Mystery appear on Last Pulls / Mercy? Recommended: no.
- Should new emoji config keys be added, or should text fallback be used initially? Needs product decision.

### Repo rules to preserve

- Do not hardcode sheet tab names, columns, or column names in feature logic.
- Tab names and schema/header mappings must stay config-driven where the project pattern supports it.
- No hidden fallbacks.
- No duplicate config paths.
- No silent legacy behavior.
- Required sheet/config fields must fail or skip safely with clear logs if missing.
