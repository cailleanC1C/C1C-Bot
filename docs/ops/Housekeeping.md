# Housekeeping jobs

Housekeeping centralizes recurring maintenance tasks that keep panel threads clean
and long-lived threads active without manual nudges.

- Feature toggles: `housekeeping_enabled` gates cleanup/keepalive scheduling; `mirralith_overview_enabled` also guards Mirralith overview posting.

## Cleanup
- **Config source.** Sheet-driven only. `HOUSEKEEPING_CLEANUP_ENABLED` must
  come from the Feature Toggles tab. Required Config-tab keys are
  `HOUSEKEEPING_CLEANUP_TAB`, `HOUSEKEEPING_CLEANUP_RUN_EVERY_HOURS`, and
  `HOUSEKEEPING_CLEANUP_DRY_RUN`; missing/invalid values prevent scheduling
  without ENV fallback or hidden defaults.
- **Targets.** Each non-empty row in the configured cleanup tab is a cleanup
  target. Required headers are `enabled`, `target_id`, `target_type`,
  `target_name`, `parent_name`, `cleanup_mode`, `min_age_hours`,
  `last_checked_at_utc`, `last_deleted_count`, `last_candidate_count`, `last_skipped_count`,
  `last_status`, and `notes`.
- **Writeback.** The bot writes only `target_type`, `target_name`,
  `parent_name`, `last_checked_at_utc`, `last_deleted_count`,
  `last_candidate_count`, `last_skipped_count`, and `last_status`. It never overwrites admin-owned
  `enabled`, `target_id`, `cleanup_mode`, `min_age_hours`, or `notes` cells.
- **Modes.** Supported cleanup modes are `all_non_pinned`,
  `bot_messages_only`, `commands_only`, and `bot_messages_and_commands`.
  Pinned messages are never deleted, and `min_age_hours` is always respected.
- **Startup validation.** Startup schedules the recurring job and also runs a
  safe validation/writeback pass so admins can see resolved target metadata and
  row status without surprise deletes.
- **Logging.** One concise summary line per run:
  - `🧹 cleanup run complete: checked_rows=<N> dry_run=<bool> deleted=<M> candidates=<C> skipped=<S> errors=<E>`
  Short WARN lines capture missing/invalid sheet configuration and API failures.

## Thread keepalive
- **Purpose.** Prevents important threads from auto-archiving when idle.
- **Config source.** Sheet-driven only. `HOUSEKEEPING_KEEPALIVE_ENABLED` must
  come from the Feature Toggles tab. Required Config-tab keys are
  `HOUSEKEEPING_KEEPALIVE_TAB`, `HOUSEKEEPING_KEEPALIVE_DEFAULT_MESSAGE`,
  `HOUSEKEEPING_KEEPALIVE_STALE_AFTER_HOURS`, and
  `HOUSEKEEPING_KEEPALIVE_RUN_EVERY_HOURS`. Missing/invalid toggle or Config
  values prevent scheduling without Config or legacy ENV fallback for the toggle.
- **Cadence vs staleness.** `HOUSEKEEPING_KEEPALIVE_RUN_EVERY_HOURS` controls
  how often the bot checks the sheet. `HOUSEKEEPING_KEEPALIVE_STALE_AFTER_HOURS`
  controls how inactive a thread must be before a keepalive message is posted.
- **Targets.** The tab named by `HOUSEKEEPING_KEEPALIVE_TAB` contains rows with
  `enabled`, `target_id`, `target_type`, `target_name`, `parent_name`,
  `keepalive_message`, `last_seen_at_utc`, `last_keepalive_sent_at_utc`,
  `last_status`, `last_checked_at_utc`, and `notes` headers.
- **Behavior.** Enabled rows may target a specific thread or a parent channel
  whose active and archived child threads are scanned. The bot writes status and
  human-readable names back to bot-owned columns without overwriting admin-owned
  `enabled`, `target_id`, `keepalive_message`, or `notes` cells.
- **Logging.** Summary per run:
  - `💙 Thread keepalive — checked_rows=<N> • posted=<N> • stale_after=<H>h • errors=<E>`
  WARN lines capture short failure details without blocking later targets.

## Role & Visitor audit
- **Purpose.** Realigns members with the expected Raid/Clan/Wandering role
  combinations and highlights Visitor records that have stalled.
- **Inputs.** `RAID_ROLE_ID`, `WANDERING_SOULS_ROLE_ID`, `VISITOR_ROLE_ID`,
  `CLAN_ROLE_IDS`, `ADMIN_AUDIT_DEST_ID`, and ticket channels
  (`WELCOME_CHANNEL_ID`, `PROMO_CHANNEL_ID`).
- **Auto-fixes.**
  - Removes Raid and adds Wandering Souls when a member has no clan tags.
  - Removes Raid from existing Wanderers that lost their clan tags.
- **Reports only.**
  - Wandering Souls that still carry clan tags.
  - Visitors without tickets, with only closed tickets, or with extra roles.
- **Delivery.** Posts one consolidated message per run to
  `ADMIN_AUDIT_DEST_ID` with section headings for each bucket.

## Future additions
AutoMod/Guardian Knight bridging will land in this module in a future phase to
keep moderation actions aligned with housekeeping cadences.

Doc last updated: 2025-12-03 (v0.9.8.2)
