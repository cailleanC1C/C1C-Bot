# C1C Bot Doc & UX Style Guide

This guide is the single source of truth for documentation conventions and the
Discord-facing presentation standards (logs, embeds, help copy). All docs and UX
surfaces should link here instead of repeating common rules.

## Logging Style

### Emoji and severity
- ✅ success / done
- ⚠️ warning / partial
- ❌ error / rejected
- ♻️ refresh / restart / cache
- 🧭 scheduler / cadence controllers
- 🐶 watchdog / keepalive
- 🔐 permissions / access controls
- 📘 lifecycle / onboarding progress
- 📋 neutral/info catch-all when none of the above apply.

### Line structure
1. **Line 1** — emoji + bold title + scope or key identifiers.
2. **Follow-up lines** — start with `•` and group related key/value pairs. Merge
   pairs on the same line using ` • ` when they describe the same bucket.
3. Keep key ordering stable between runs for rapid visual diffing.
4. Prefer resolved labels over numeric IDs. Helpers automatically fall back to
   `#unknown` labels when Discord cache misses occur.
5. Humanize values: `fmt_duration` for seconds/minutes/hours, `fmt_count` for
   thousands separators, and `fmt_datetime` for UTC timestamps.
6. Hide empty values behind `-` and avoid repeating context already implied by
   the emoji/title (e.g., don’t repeat `scheduler` when the emoji is 🧭).

### Canonical examples
```
🧭 **Scheduler** — intervals: clans=3h • templates=7d • clan_tags=7d • onboarding_questions=7d
• clans=2025-11-17 21:00 UTC
• templates=2025-11-20 00:00 UTC
• clan_tags=2025-11-20 00:00 UTC
• onboarding_questions=2025-11-20 00:00 UTC

✅ **Guild allow-list** — verified • allowed=[C1C Cluster] • connected=[C1C Cluster]
❌ **Guild allow-list** — violation • connected=[Other Guild] • allowed=[C1C Cluster]

📘 welcome_panel_open — ticket=W0488-smurf • actor=@Recruit
• channel=#WELCOME CENTER › welcome • questions=16
```
Structured JSON/stdout logs remain unchanged; only Discord-facing helpers follow
this UX format.

## Embed & Panel Style

### Titles & descriptions
- Titles include an emoji or badge plus a terse scope (e.g., `🔥 C1C • Recruitment Summary`).
- Descriptions are optional; reserve them for one-sentence callouts or warnings.
- Keep ticket/thread/channel references human readable. Prefer `#CHANNEL › thread`
  over raw IDs.

### Status rows & inline messaging
- Inline status rows ("waiting", "saved", "error") appear inside the embed body
  unless the surface requires a separate follow-up message. Mention the actor and
  latest action for quick scanning.
- When embeds represent a wizard/panel, the persistent message carries the live
  state; avoid emitting multiple status embeds unless specified by that flow.

### Fields, inline pairs, and formatting
- Use bold labels (`**Label:** value`) inside fields for readability.
- Pair related answers on a single line separated by ` • ` when they share a
  context (e.g., `**Power:** … • **Bracket:** …`).
- Collapse optional sections when data is empty. Follow each surface’s hide rules
  (see Welcome Summary Spec for the canonical approach).
- Keep within Discord limits (25 fields per embed, 1024 characters per field,
  6000 characters total). Split across multiple embeds only when content exceeds
  those limits.

### Colours, icons, and assets
- Colours come from `shared.theme` helpers (no hardcoded hex values).
- Thumbnails/avatars are optional. Use them only when the flow supplies a stable
  asset (e.g., clan crest, recruit avatar).
- Embed footers always include the running versions or relevant timestamp as
  defined in this guide’s Documentation Conventions.

### Panels & controls
- Discord panels must keep controls within five component rows (four selects +
  one button row is the common layout).
- Persist panels via edit-in-place updates to avoid flooding channels.
- Provide recovery affordances (restart/resume buttons) that match the logging
  semantics (♻️ restart vs 📘 lifecycle).

## Help & Command Text Style
- Command copy originates from `docs/_meta/COMMAND_METADATA.md`; update that
  export first, then propagate to embeds and docs.
- Tone: concise, direct, written in the imperative (“Run `!ops refresh` after …”).
- Usage strings show literal syntax (`Usage: !command [options]`). Optional args
  live in brackets, mutually exclusive flags are spelled out.
- Every help embed lists Tier, Detail, and a short Tip. Tips focus on operator
  behavior, not implementation notes.
- Mention surfaces use the same copy as prefix commands (e.g., `@Bot ping`).
- Footers show the version string only (`Bot vX.Y.Z · CoreOps vA.B.C • For details: @Bot help`).
- The overview help message always sends four embeds (Overview, Admin/Operational,
  Staff, User) and hides empty sections unless `SHOW_EMPTY_SECTIONS=true`.

## Documentation Conventions

### Titles & headings
- Each markdown file starts with a stable `#` H1. Do not include temporary code
  names or delivery phases in titles.
- Maintain logical heading nesting (H2 for primary sections, H3/H4 for detail).

### Footer contract
- Final line must read `Doc last updated: yyyy-mm-dd (v0.9.x)`.
- No blank lines after the footer. Use the bot version listed in the root README.

### Environment source of truth
- Reference environment variables via [`docs/ops/Config.md`](../ops/Config.md#environment-keys).
- `.env.example` must contain the same key set as the Config table (order may differ).

### Index discipline
- [`docs/README.md`](../README.md) lists every markdown file in `/docs`. Update it
  whenever files are added, removed, or renamed.

### Automation
- Run `python scripts/ci/check_docs.py` (or `make -f scripts/ci/Makefile docs-check`)
  before opening a PR. The checker validates titles, footers, index coverage,
  ENV parity, and in-doc links.

## References
- [`docs/ops/Logging.md`](../ops/Logging.md) — technical logging configuration,
  dedupe policy, and helper wiring.
- [`docs/modules/Welcome.md`](../modules/Welcome.md) — owner of the welcome
  panels/wizard, summary layout, and recruiter embeds.
- [`docs/ops/CommandMatrix.md`](../ops/CommandMatrix.md) — runtime layout of the
  help system.

Doc last updated: 2025-12-31 (v0.9.8.2)
