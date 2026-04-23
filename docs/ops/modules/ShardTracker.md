# Shard & Mercy Tracker Module

Community-focused module that lets members maintain their RAID shard stash and
mercy counters directly in Discord. Every interaction happens inside the
dedicated **Shards & Mercy** channel so global chatter stays clean while users
keep a private, button-driven tracker thread.

## Scope & Responsibilities

- Persist shard stash counts, last pulls, and mercy counters (`ancient`,
  `void`, `sacred`, `primal`) per Discord user, including separate Legendary
  and Mythical mercy tracks for Primals.
- Enforce channel routing: only the configured Shards & Mercy channel may run
  shard commands, and every user gets a private thread underneath that channel.
- Surface a mobile-friendly, tabbed embed with shard-icon buttons and a
  two-phase mercy bar (green/white toward mercy, orange/black when past it) so
  players can log pulls or add shards without typing commands. Panel buttons are
  persistent and survive bot restarts.
- Provide modal-based logging for Legendary/Mythical pulls and manual stash
  setters via `!shards set …` plus a shared `!help shards` footer to explain
  the controls.
- Log lifecycle, warning, and error states through the existing C1C log helper
  plus ADMIN_ROLE_IDS pings on hard failures.

> Cleanup/auto-archive of shard threads is handled in a follow-up PR. This
> module only creates and reuses per-user threads; they persist indefinitely
> today.

## Storage & Configuration

- **Sheet:** `MILESTONES_SHEET_ID` (service account credentials already live in
  `GSPREAD_CREDENTIALS`).
- **Config tab:** `MILESTONES_CONFIG_TAB` (defaults to `Config`). Required keys:
  - `SHARD_MERCY_TAB` — worksheet name that stores shard rows.
  - `SHARD_MERCY_CHANNEL_ID` — numeric ID for the dedicated Discord channel.
  - `SHARD_CLANS_TAB` — worksheet name for clan sharing + weekly reminders.
  - `SHARD_REMINDER_TAB` — worksheet for durable weekly reminder sent tracking.
- **Worksheet schema:** row 1 contains the canonical headers listed below.
  `discord_id` is the primary key and all headers must remain in this order.

| Column | Purpose |
| --- | --- |
| `discord_id` | Snowflake ID for the Discord user. |
| `username_snapshot` | Last display name captured when the bot touched the row. |
| `ancients_owned`, `voids_owned`, `sacreds_owned`, `primals_owned` | Current stash counts. |
| `ancients_since_lego`, `voids_since_lego`, `sacreds_since_lego`, `primals_since_lego` | Mercy counters per shard type. |
| `primals_since_mythic` | Mythical mercy counter. |
| `last_*_lego_iso`, `last_primal_mythic_iso`, `last_updated_iso` | UTC ISO timestamps for the last logged Legendary/Mythical and update time. |
| `last_*_lego_depth`, `last_primal_mythic_depth` | Mercy depth at the time the pull was logged. |

Rows are created automatically the first time a user opens the tracker; all
fields initialize to zero and timestamps stay blank until a LEGO is recorded.

### SHARD_CLANS_TAB schema

Required headers:

`clan_key`, `enabled`, `share_channel_id`, `share_thread_id`, `reminder_enabled`,
`opt_in_role_id`, `reminder_day`, `reminder_time_utc`, `Title`, `Body`,
`Footer`, `ColorHex`, `EmojiNameOrId`

## Mercy Math Reference

Plarium’s official mercy values:

| Type | Base chance | Mercy threshold | Increment |
| --- | --- | --- | --- |
| Ancient Legendary | 0.5% | After 200 shards | +5% per shard |
| Void Legendary | 0.5% | After 200 shards | +5% per shard |
| Sacred Legendary | 6% | After 12 shards | +2% per shard |
| Primal Legendary | 1% | After 75 shards | +1% per shard |
| Primal Mythical | 0.5% | After 200 shards | +10% per shard |

Chance increases after crossing the threshold and caps at 100%. Mythical pulls
reset both primal tracks; Legendary pulls reset only the Legendary track but the
Mythical counter continues accumulating primal shards.

## Commands & Buttons

| Command | Notes |
| --- | --- |
| `!shards [type]` | Posts the tabbed shard tracker in the user’s shard thread. Optional `type` opens a detail tab. |
| `!shards set <type> <count>` | Force-set a stash count (non-negative integer). |

All commands **must** be issued in `<#SHARD_MERCY_CHANNEL_ID>`. When a member
runs a command directly in that channel, the bot creates (or reuses) their
personal thread named `Shards – <Display Name> [user_id]`, posts the embed
there, and replies in the parent channel with a short pointer.

Buttons live on every embed inside the user’s thread and are **locked to the
thread owner**:

- **Tab buttons** — Text-only Overview tab plus emoji-only shard tabs
  (Ancient/Void/Sacred/Primal) and a text Last Pulls tab.
- **+ Stash / - Pulls** — open numeric modals on the active shard tab only.
  + Stash increases stash without touching mercy; - Pulls reduces stash (floored
  at 0) and increments mercy. Primal pull logging increments Legendary and
  Mythical mercy separately so Mythical pulls no longer reset Legendary mercy.
- **Got Legendary** — shard tabs only. Non-Primal resets the legendary mercy to
  zero; Primal opens a follow-up with Legendary vs Mythical to decide whether to
  reset only the legendary counter or both mercy tracks.

Only the thread owner may press the buttons; everyone else receives a friendly
rejection message. Button handlers write through to Sheets immediately and edit
the message in place.

## Thread Workflow

1. **First use:** the member types `!shards` in the configured channel. The bot
   creates a private thread, posts the dashboard there, and mentions the user in
   the parent channel with a “📬 Posted in your shard thread” pointer.
2. **Returning users:** commands or buttons run inside the thread directly, or
   the user can type another command in the parent channel; the bot replies in
   the existing thread to keep the parent channel clean.
3. **Finding the thread later:** the channel’s thread list shows
   `Shards – <Display Name> [user_id]`. Members can also re-run `!shards` in the
   parent channel; the bot links the thread again instead of duplicating it.

## Logging & Errors

- Lifecycle events (`📘 Shards — …`) post via `runtime.send_log_message`. Each
  action includes the user label plus a concise description.
- Hard errors (missing config tab, header mismatch, invalid channel) reply to
  the user with a polite explanation and ping the first ADMIN_ROLE_ID inside a
  `❌` log entry.
- All Sheets writes run through the shared async backoff helper and use a
  single-row range update (`A{row}:V{row}`) so the schema stays intact.

## Testing Expectations

Automated tests cover:

- Mercy math for every shard type and the primal mythic pity.
- Sheet mapping (row ↔ dataclass) to ensure headers remain stable.
- Command parsing helpers (`type` aliases, channel gating).
- Thread routing unit tests verifying channel restrictions and reusing an
  existing thread before creating a new one.

Doc last updated: 2025-11-20 (v0.9.8.2)
