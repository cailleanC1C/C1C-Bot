# Live Arena tournament lifecycle

Tournament identity is split into three fields:

- `tournament_id`: immutable internal identity
- `tournament_name`: full public display name
- `tournament_short_name`: compact public label

`ACTIVE_TOURNAMENT_ID` remains the convenience pointer for the current workflow, but historical and draft tournament rows may coexist in `TOURNAMENTS`.

Tournament statuses are `draft`, `signup_open`, `signup_closed`, `active`, `completed`, and `archived`.

Tournament-level Discord resources are stored in the CONFIG-routed `TOURNAMENT_DISCORD_RESOURCES` table and keyed by `(tournament_id, resource_type, resource_key)`. Round overview IDs remain owned by `ROUNDS`; matchup thread IDs remain owned by `MATCHES`.

Legacy `PUBLIC_PANEL_MESSAGE_ID` and `ORGANIZER_PANEL_MESSAGE_ID` CONFIG values are compatibility fallbacks only. Once a tournament resource row exists, the resource registry is authoritative.

Archival preserves Sheet rows and Discord history. Tournament-level resources are retired rather than deleted, Victory Ledger content is left untouched, and match-thread ownership remains on `MATCHES`.

Doc last updated: 2026-08-10 (v0.9.8.3)
