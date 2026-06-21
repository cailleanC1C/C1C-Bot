# Housekeeping jobs

Housekeeping centralizes recurring maintenance tasks that keep panel threads clean
and long-lived threads active without manual nudges.

- Feature toggles: `housekeeping_enabled` gates cleanup/keepalive scheduling; `mirralith_overview_enabled` also guards Mirralith overview posting.

## Cleanup
- **Scope.** Deletes every non-pinned message in configured threads so panels reset
  each run. Pinned messages are never removed.
- **Cadence.** Runs every `CLEANUP_INTERVAL_HOURS` (default: 24h).
- **Targets.** Threads enumerated via `CLEANUP_THREAD_IDS`.
- **Logging.** One summary line per run:
  - `🧹 Cleanup — threads=<N> • messages_deleted=<M> • errors=<E>`
- **Error handling.** Missing permissions or API failures are logged as WARN lines
  and counted in the `errors` field, but the job continues to the next thread.

## Thread keepalive
- **Purpose.** Prevents important threads from auto-archiving when idle.
- **Config source.** Sheet-driven only, using the recruitment/Mirralith workbook
  Config tab intentionally. Required Config-tab keys are
  `HOUSEKEEPING_KEEPALIVE_ENABLED`, `HOUSEKEEPING_KEEPALIVE_TAB`,
  `HOUSEKEEPING_KEEPALIVE_DEFAULT_MESSAGE`,
  `HOUSEKEEPING_KEEPALIVE_STALE_AFTER_HOURS`, and
  `HOUSEKEEPING_KEEPALIVE_RUN_EVERY_HOURS`.
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
