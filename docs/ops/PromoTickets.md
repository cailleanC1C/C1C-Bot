# Promo tickets

Promo tickets track returning players and clan move requests. Ticket Tool names
follow the `R/M/L` prefix patterns:

- `R####-username` — returning player tickets
- `M####-username` — member/player move requests
- `L####-username` — clan lead move requests

Optional clan tags may trail the username (e.g., `R0123-user-CLAN`).

## Logging

The promo watcher (`modules.onboarding.watcher_promo.PromoTicketWatcher`):

- Detects threads under `PROMO_CHANNEL_ID` without checking thread owners.
- Parses promo ticket names to extract `ticket number`, `username`, and optional
  `clantag`.
- Logs opens and closes to the `PROMO_TICKETS_TAB` worksheet using the following
  columns:

  `ticket number | username | clantag | source_clan_tag | date closed | type | thread created |
  year | month | join_month | clan name | progression`

- Maps prefixes to types: `R` → `returning player`, `M` → `player move request`,
  `L` → `clan lead move request`.
- On closure, prompts the closer for both **where the member came from** (`source_clan_tag`)
  and **where the member is going** (`clantag`). Successful close math releases one
  source open spot and consumes one destination open spot, unless the normalized
  source and destination are the same clan or either side is `NONE`.
- Destination reservations still prevent double-consuming the destination spot,
  while source-clan release still runs for member moves.
- Lifecycle logs surface as `Promo panel — scope=promo` entries; welcome only
  handles threads that begin with `W####-…`.

## Configuration

- **Channels:** `PROMO_CHANNEL_ID`
- **Sheet tab:** `PROMO_TICKETS_TAB`
- **Required Promo source header:** Config key `PROMO_SOURCE_CLAN_TAG_HEADER=source_clan_tag`; the resolved header must exist in `PROMO_TICKETS_TAB`.
- **Toggles:** `PROMO_ENABLED`, `ENABLE_PROMO_HOOK` (promo dialog toggle
  reserved for later: `promo_dialog`)

## Onboarding hooks

- Ticket Tool greetings in promo threads must retain the hidden trigger line at
  the bottom of the template:
  - `<!-- trigger:promo.r -->` — Returning Player
  - `<!-- trigger:promo.m -->` — Member / Player Move Request
  - `<!-- trigger:promo.l -->` — Leadership Move Request
- When `PROMO_ENABLED` and `ENABLE_PROMO_HOOK` are on, the bot reacts to the
  trigger, posts the Open Questions panel, and launches the corresponding promo
  flow when the panel is opened.
- Recruiters can also add 🎫 to the Ticket Tool greeting to surface the panel if
  the watcher misses the trigger. Removing the trigger lines prevents the bot
  from recognising promo tickets.

Doc last updated: 2026-06-08 (v0.9.8.2)
