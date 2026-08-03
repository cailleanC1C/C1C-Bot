# ADR-0023 — C1C Leagues Autoposter
Date: 2025-12-01

## Context
- Weekly league leaderboard images for Legendary, Rising Stars, and Stormforged were being posted manually from the C1C_Leagues sheet.
- We need a reliable, sheet-driven autoposter that mirrors the Mirralith Config-tab pattern without adding a database or new polling loops.
- Free-tier constraints require lightweight scheduling and reuse of existing CoreOps helpers for Sheets access, logging, and scheduling.

## Decision
- Introduce a Leagues cog under `modules.community.leagues` that:
  - Watches `LEAGUES_SUBMISSION_CHANNEL_ID` for image uploads and assigns `C1C_LEAGUE_ROLE_ID` on first submission.
  - Reads league header/body specs from the `C1C_Leagues` sheet Config tab (`LEAGUES_SHEET_ID`/`LEAGUES_CONFIG_TAB`).
  - Sends Monday and Wednesday reminders via scheduled jobs; Wednesday stores the message ID for 👍 confirmation by `LEAGUE_ADMIN_IDS`.
  - Runs an atomic posting pipeline that exports all ranges to PNGs, posts to the three league threads, and then drops a single announcement into `ANNOUNCEMENT_CHANNEL_ID`.
  - Treats all league board counts as config-driven: any `LEAGUE_<SLUG>_<N>` rows present in the Config tab are exported and posted in numeric order.
  - Requires each configured league to have a header and at least one board; fixed per-league counts are no longer enforced in code.
  - Uses the header posts as the anchor for announcement jump links; each board image is posted as its own message beneath the header.
- Scheduler wiring uses `LEAGUES_REMINDER_MONDAY_UTC` and `LEAGUES_REMINDER_WEDNESDAY_UTC` (UTC) with the existing Runtime scheduler; failures log softly without blocking startup.
- No new persistence layer is introduced; state remains in ENV and the Leagues Config tab.

## Consequences
- New env keys document Leagues sheet/thread IDs, admin allow-list, reminder times, and the shared @C1CLeague role/announcement channel.
- Weekly postings are consistent and fail-atomic; partial exports or missing targets stop the run with a clear status message in the reminder thread.
- Future leagues can reuse the same Config-tab grouping pattern without architectural changes.

Status: Approved
## Weekly cluster history capture
- After league bundle validation and before the first export or Discord send, the posting pipeline captures an append-only weekly snapshot in `ClusterEventHistory`. A validation, alias, negative-delta, or history-conflict failure stops the job before any board or announcement is posted. A Discord failure after capture does not remove history; retries safely deduplicate identical record keys.
- The Config keys `cluster_capture_config_tab`, `cluster_clan_map_tab`, `cluster_event_history_tab`, and `cluster_evaluation_tab` resolve every prepared tab name. Capture specs supply source worksheet names, ranges, column letters, modes, units, and result-only status; table fields are resolved by header.
- Active clan aliases are punctuation/whitespace-insensitive. Every active clan receives one candidate per enabled spec, while former/unmapped source clans are ignored. Missing, invalid, and zero weekly scores remain blank with `evaluation_status=missing`—they are never recorded as zero.
- `weekly_score` supports positive score snapshots. `cumulative_win_delta` records wins/losses and preserves the wins delta as `result_only`; negative deltas abort the capture. Current CvC/Siege sources do not provide cycle dates, participation, action counts, event class, or enough data for complete mode ratings, so those fields remain blank. Source cells are never reset or cleared.

Doc last updated: 2026-08-03 (v0.9.8.2)
