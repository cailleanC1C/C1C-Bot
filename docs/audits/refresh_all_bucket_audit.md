# Refresh-all sheet bucket audit

Date: 2026-06-29

## Registry location

`!refresh all` reads `shared.cache.telemetry.list_buckets()`, which reflects buckets registered in the shared cache service. Sheet-backed default registration is coordinated by `shared.sheets.runtime.register_default_cache_buckets()`.

## Buckets that existed before this PR

- `clans` — recruitment clan roster cache.
- `templates` — welcome template cache.
- `clan_tags` — onboarding clan tag cache.
- `onboarding_questions` — onboarding question cache.
- `leagues` — league Config cache.
- `fusion` — Fusion row cache.
- `fusion_events` — Fusion event row cache.
- `reaction_roles` — reaction role cache.

## Added bucket Config sources

| Bucket | Config key | Config source | Evidence |
|---|---|---|---|
| `clan_ad_messages` | `clan_ad_messages_tab` | Recruitment Config | `modules.recruitment.clan_ads.CONFIG_KEYS` |
| `clan_ad_rules` | `clan_ad_rules_tab` | Recruitment Config | `modules.recruitment.clan_ads.CONFIG_KEYS` |
| `reservations` | `reservations_tab` | Recruitment Config | `shared.sheets.recruitment.get_reservations_tab_name()` |
| `recruitment_reports` | `reports_tab` | Recruitment Config | `shared.sheets.recruitment.get_reports_tab_name()` |
| `c1c_ad` | `C1C_AD_TAB` | Recruitment Config | `modules.housekeeping.c1c_ad.CONFIG_KEYS` |
| `c1c_ad_text` | `C1C_AD_TEXT_TAB` | Recruitment Config | `modules.housekeeping.c1c_ad.CONFIG_KEYS` |
| `cleanup_rules` | `HOUSEKEEPING_CLEANUP_TAB` | Recruitment Config | `modules.housekeeping.cleanup.CONFIG_TAB` |
| `keepalive_targets` | `HOUSEKEEPING_KEEPALIVE_TAB` | Recruitment Config | `modules.housekeeping.keepalive.CONFIG_TAB` |
| `whoweare_role_map` | `rolemap_tab` | Recruitment Config | `shared.sheets.recruitment.get_role_map_tab_name()` |
| `reset_reminders` | `RESET_REMINDER_TAB` | Runtime/shared config merged from Milestones Config | `modules.community.reset_reminders.scheduler._RESET_REMINDER_TAB_KEY` |
| `shard_mercy` | `SHARD_MERCY_TAB` | Runtime/shared config merged from Milestones Config | `modules.community.shard_tracker.data.ShardSheetStore.get_config()` |
| `shard_clans` | `SHARD_CLANS_TAB` | Runtime/shared config merged from Milestones Config | `modules.community.shard_tracker.data._config_tab_name("SHARD_CLANS_TAB")` |
| `shard_share_copy` | `shard_share_copy_tab` | Runtime/shared config merged from Milestones Config | `modules.community.shard_tracker.data._config_tab_name("shard_share_copy_tab")` |
| `shard_voice_targets` | `shard_share_voice_targets_tab` | Runtime/shared config merged from Milestones Config | `modules.community.shard_tracker.data._config_tab_name("shard_share_voice_targets_tab")` |

## Candidate audit results

| Candidate | Status | Reason |
|---|---|---|
| Clans | Already covered by existing bucket | `shared.sheets.recruitment.register_cache_buckets()` registers `clans`. |
| Clan Tags | Already covered by existing bucket | `shared.sheets.onboarding.register_cache_buckets()` registers `clan_tags`. |
| Welcome/Templates | Already covered by existing bucket | `shared.sheets.recruitment.register_cache_buckets()` registers `templates`. |
| Onboarding Questions | Already covered by existing bucket | `shared.sheets.onboarding_questions.register_cache_buckets()` registers `onboarding_questions`. |
| Reaction Roles | Already covered by existing bucket | `shared.sheets.reaction_roles.register_cache_buckets()` registers `reaction_roles`. |
| Leagues | Already covered by existing bucket | `shared.sheets.config_service.register_cache_buckets()` registers `leagues`. League-specific tabs are resolved from that Config cache. |
| Fusion rows | Already covered by existing bucket | `shared.sheets.fusion.register_cache_buckets()` registers `fusion`. |
| Fusion events | Already covered by existing bucket | `shared.sheets.fusion.register_cache_buckets()` registers `fusion_events`. |
| ClanAdMessages | Missing and added in this PR | Uses existing `clan_ad_messages_tab` from Recruitment Config. |
| ClanAdRules | Missing and added in this PR | Uses existing `clan_ad_rules_tab` from Recruitment Config. |
| Reservations | Missing and added in this PR | Uses existing `reservations_tab` from Recruitment Config. |
| Recruitment reports | Missing and added in this PR | Uses existing `reports_tab` from Recruitment Config. |
| C1C ad image tab | Missing and added in this PR | Uses existing `C1C_AD_TAB` from Recruitment Config. |
| C1C ad text tab | Missing and added in this PR | Uses existing `C1C_AD_TEXT_TAB` from Recruitment Config. |
| Cleanup rules | Missing and added in this PR | Uses existing `HOUSEKEEPING_CLEANUP_TAB` from Recruitment Config. |
| Keepalive targets | Missing and added in this PR | Uses existing `HOUSEKEEPING_KEEPALIVE_TAB` from Recruitment Config. |
| WhoWeAre role map | Missing and added in this PR | Uses existing `rolemap_tab` from Recruitment Config. |
| Reset reminders | Missing and added in this PR | Uses existing `RESET_REMINDER_TAB` from Milestones Config merged into runtime/shared config. |
| Shard mercy | Missing and added in this PR | Uses existing `SHARD_MERCY_TAB` from Milestones Config merged into runtime/shared config. |
| Shard clans | Missing and added in this PR | Uses existing `SHARD_CLANS_TAB` from Milestones Config merged into runtime/shared config. |
| Shard reminders | Intentionally not refreshable because not cache-backed | `SHARD_REMINDER_TAB` is the shard tracker weekly reminder dedupe/writeback ledger read by `get_sent_weekly_reminder_keys()` and written by `mark_weekly_reminder_sent()`, not a shared cache bucket. |
| Shard share copy | Missing and added in this PR | Uses existing `shard_share_copy_tab`/normalized `SHARD_SHARE_COPY_TAB` from runtime/shared config. |
| Shard voice targets | Missing and added in this PR | Uses existing `shard_share_voice_targets_tab`/normalized `SHARD_SHARE_VOICE_TARGETS_TAB` from runtime/shared config. |
| Fusion user progress | Intentionally not refreshable because not cache-backed | `FUSION_USER_EVENT_PROGRESS_TAB` is a mutable progress/writeback ledger, not a shared cache bucket. |
| Fusion reminder/dedupe/state tabs | Intentionally not refreshable because not cache-backed | Reminder/settings/state tabs are runtime dedupe and writeback paths, not safe as generic read-only manual warmers. |
| Mirralith overview tabs | Intentionally not refreshable because not cache-backed | `MIRRALITH_TAB`/`CLUSTER_STRUCTURE_TAB` drive image export ranges; the module does export work, not cache-service tab warming. |
| Placement / target select tabs | Not sheet-backed | The current module is a stub and only logs during setup; no commands or sheet tab reads exist. |
| Server map / permissions tabs | Intentionally not refreshable because not cache-backed | These are diagnostic/state paths and not registered cache-service feature buckets. |
| Onboarding sessions/watchers | Intentionally not refreshable because not cache-backed | Session/ticket tabs are runtime state/writeback paths; question data is already covered by `onboarding_questions`. |

## Expected `!refresh all` output after this PR

The exact table status depends on live Config and Sheets access. The bucket list should include the existing buckets plus the added readable labels below, with each row reporting `ok`, `fail`, retry/TTL/count details when available, and visible errors for missing Config keys or bad tab names:

- C1C Ad
- C1C Ad Text
- Clan Ad Messages
- Clan Ad Rules
- Cleanup Rules
- Keepalive Targets
- Recruitment Reports
- Reservations
- Reset Reminders
- Shard Clans
- Shard Mercy
- Shard Share Copy
- Shard Voice Targets
- WhoWeAre Role Map

Duplicate bucket registration is prevented by checking `cache.get_bucket(name)` before registering, and tests assert that `list_buckets()` contains no duplicate names.
