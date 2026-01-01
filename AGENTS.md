# Agents Registry

> Single source of truth for all non-human actors (bots, service accounts, webhooks, schedulers).  
> Store **secret names only** (ENV var keys), never secret values. Update this file in the same PR as any permission/scope change.

---

## 1) Directory

| Agent | Type | Primary Purpose | Environments | Owner | Notes |
|---|---|---|---|---|---|
| The Woadkeeper | Discord Bot (discord.py) | Recruitment, onboarding, ops | prod, test | Caillean | Unified bot; replaces legacy modules |
| Google Sheets SA | Service Account | Sheets R/W (config, toggles, reports) | prod, test | Caillean | Least-privilege per sheet |
| Ticket Tool (external) | External Bot | Ticket threads (welcome flow) | prod | 3rd party | Thread owner in private tickets |

---

## 2) The Woadkeeper (Discord Bot) 

docs/ops/.env.example

**Identifiers / Secrets (ENV var names — exact):**
- `DISCORD_TOKEN` — bot token used at runtime
- `GSPREAD_CREDENTIALS` / `GOOGLE_SERVICE_ACCOUNT_JSON` — service account credentials used for Sheets access

**Sheets / Doc IDs (ENV var names — exact):**
- `RECRUITMENT_SHEET_ID`
- `ONBOARDING_SHEET_ID`
- `REMINDER_SHEET_ID`
- `MILESTONES_SHEET_ID`
- *(other sheet IDs live in env.example — keep doc updates in sync with env.example when adding new sheets)*

**Logging / Telemetry (ENV var names — exact):**
- `LOG_CHANNEL_ID`
- `LOG_LEVEL`
- `BOT_NAME`
- `BOT_VERSION`
- `ENV_NAME`

**Feature Toggles / Config (from Sheets, not ENV)**
- Feature toggles must be served from your sheet config tabs (per contract). Do **not** move runtime toggles to ENV unless you explicitly add them to `env.example` and document the reason.

**Gateway Intents / Permissions (operational guidance)**
- Maintain minimum necessary intents in both portal and code. Any change to intents or OAuth scopes must be:
  1. Listed here,
  2. Justified in the PR body,
  3. Reflected in CI/guardrails as needed.

**RBAC / Access Lists**
- Permissions UI blacklists: `PERMS_BLACKLIST_CHANNEL_IDS`, `PERMS_BLACKLIST_CATEGORY_IDS` — **IDs only**; numeric; comma-separated if needed.
- `GUILD_IDS` env var exists for allowed guilds.

**Deploy / Runtime**
- Host: Render (env groups `prod`, `test` controlled by `ENV_NAME` + Render environment groups)
- Health pings: controlled by watchdog vars (see below)
- Public URL / rendering:
  - `PUBLIC_BASE_URL`
  - `RENDER_EXTERNAL_URL`

---

## 3) Google Sheets Service Account

**Identity / Secrets (ENV var names — exact):**
- `GOOGLE_SERVICE_ACCOUNT_JSON`
- `GSPREAD_CREDENTIALS`

**Access**
- Read/Write: recruitment/config/milestones/reminders (sheet IDs above)
- Principle: No hard-coded tab names; resolve tab names and IDs from your Sheet Config tabs.

---

## 4) Watchdog / Scheduler / Operational variables (ENV var names — exact)
Use these to reason about health and lifecycle; if you change them, reflect changes here:

- `WATCHDOG_CHECK_SEC`
- `WATCHDOG_STALL_SEC`
- `WATCHDOG_DISCONNECT_GRACE_SEC`
- `KEEPALIVE_INTERVAL_SEC`
- `REFRESH_TIMES`
- `REPORT_DAILY_POST_TIME`

---

## 5) Misc runtime and UI tuning (ENV var names — exact)
- `TIMEZONE`
- `REFRESH_TIMES`
- `CLAN_TAGS_CACHE_TTL_SEC`
- `CLEANUP_INTERVAL_HOURS`
- `EMOJI_MAX_BYTES`
- `EMOJI_PAD_SIZE`
- `EMOJI_PAD_BOX`
- `TAG_BADGE_PX`
- `TAG_BADGE_BOX`
- `STRICT_EMOJI_PROXY`
- `PUBLIC_BASE_URL`
- `RENDER_EXTERNAL_URL`

---

## 6) Guardrails & Change Control (process)
docs/guardrails/RepositoryGuardrails.md
- Any change to **intents, OAuth scopes, permissions, access lists, or sheet IDs** must:
  1) Be reflected here,
  2) Include a short rationale in the PR body,
  3) Update CI/Guardrails config if applicable (and update the CollaborationContract Appendix A reference if guardrails change).
- Do not invent labels — follow `.github/labels/labels.json`.
- **AUDIT/** is excluded from tests/analysis (except for generated audit outputs).

---

## 7) Codex Hook (read this, do not duplicate policy)

For PR formatting rules, labels, and doc footers see `docs/contracts/CollaborationContract.md — Appendix A (Codex Operating Standards)`. This file is *only* the agent registry and must not duplicate the operating standards.

---

## 8) Validation Checklist (used during PRs)

- [ ] `DISCORD_TOKEN` present in env.example (no value in repo).
- [ ] `GSPREAD_CREDENTIALS` or `GOOGLE_SERVICE_ACCOUNT_JSON` present in env.example.
- [ ] Sheet ID env entries present in env.example for any sheet used (e.g., `RECRUITMENT_SHEET_ID`, `ONBOARDING_SHEET_ID`, etc.).
- [ ] `PERMS_BLACKLIST_CHANNEL_IDS` / `PERMS_BLACKLIST_CATEGORY_IDS` set when exclusions are required (IDs only).
- [ ] `LOG_CHANNEL_ID` present in env.example and used by logging code.
- [ ] Any change to access/intents documented in PR and this file updated in same PR.

---

Doc last updated: 2025-12-31 (v0.9.8.2)
