# Sheets & Config Reference

## Source-of-truth pattern

1. **ENV bootstraps the service:** secrets, workbook/document IDs, guild/core IDs, deployment identity, logging, public URL, and watchdog timing. Secret values never belong in Git.
2. **Each workbook's Config tab routes features:** it names feature tabs, message/channel/role destinations, schedules, and other operational values. Do not hard-code tab names when a Config key exists.
3. **Feature Toggles gate behavior:** runtime feature gates come from Sheets, not ENV, unless the repository contract explicitly documents an ENV exception.
4. **Feature tabs carry definitions/state:** examples include clans, templates, questions, cleanup targets, reminders, reservations, achievement definitions, ads, and guide records.
5. **Code resolves columns by headers and aliases:** column order is not an API. Preserve required headers; supported aliases provide migrations, not permission to invent schema.

**Codex and other code agents must not edit live Google Sheets.** They may update repository documentation or code only. A human Sheet owner performs live tab/header/config/toggle edits and validates them operationally.

## ENV categories

- Credentials: `DISCORD_TOKEN`, `GOOGLE_SERVICE_ACCOUNT_JSON` or `GSPREAD_CREDENTIALS`.
- Workbook IDs: `RECRUITMENT_SHEET_ID`, `ONBOARDING_SHEET_ID`, `REMINDER_SHEET_ID`, `MILESTONES_SHEET_ID`, `ACHIEVEMENTS_SHEET_ID`, plus any documented IDs in `docs/ops/.env.example`.
- Runtime identity/logging: `ENV_NAME`, `BOT_NAME`, `BOT_VERSION`, `LOG_LEVEL`, `LOG_CHANNEL_ID`, `GUILD_IDS`.
- Discord core/access IDs: permission blacklists, Wandering Souls roles, and other documented role/channel IDs.
- Lifecycle: watchdog, keepalive/public URL, timezone, refresh/report timing.

Never expose credential values in `config`, `env`, logs, screenshots, issues, or wiki pages.

## Safe Sheet change procedure

1. Confirm workbook, Config pointer, toggle, headers, aliases, and destination IDs.
2. Make one bounded live Sheet change with the Sheet owner.
3. Run `!checksheet --debug` only in an approved admin channel; correct missing tabs/headers.
4. Use `!refresh <bucket>` for cached feature data. Use `!reload onboarding` for onboarding schema questions. Use full reload/restart only when the runbook calls for it.
5. Exercise a non-destructive preview or dry-run and check logs/rate limits.

## Ownership and writes

Some tabs mix admin-owned inputs and bot-owned status columns. The bot must only write its documented columns. Cleanup, keepalive, reservations, onboarding sessions, and reports have specialized schemas; consult the corresponding detailed docs under `docs/ops/` and ADRs before changing headers.

Doc last updated: 2026-07-20 (v0.9.8.2)
