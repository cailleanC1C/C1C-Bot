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

Doc last updated: 2025-12-02 (v0.9.8.2)
