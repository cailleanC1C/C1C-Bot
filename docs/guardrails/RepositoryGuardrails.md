# **Repository Guardrails — Master Specification**

**Purpose:** Single source of truth for all constraints that govern this codebase.
Every audit and CI check validates against this document.

---

## **1) Repository Structure**

- **S-01 Modules-First:** All features live under `modules/<domain>` (e.g., `modules/recruitment`, `modules/placement`, `modules/onboarding`). No top-level feature folders.
- **S-02 Shared = Infra Only:** `shared/` contains cross-bot infrastructure (sheets, cache, telemetry, small pure helpers). No Discord commands, views, or RBAC logic in `shared/`.
- **S-03 Cogs-Only Registration:** All commands register under `cogs/`. No `@commands.command`, `@bot.command`, or `@tree.command` outside `cogs/`.
- **S-04 No Side-Effect Imports:** Importing any `modules/*` file must not register commands, start tasks, or mutate runtime globally.
- **S-05 Single Home per Domain:** Each domain has one canonical home (e.g., CoreOps). No duplicate implementations across `shared/`, `modules/`, or `packages/`.
- **S-06 Packages for Reuse:** Reusable feature-level code lives in `packages/<name>` with `pyproject.toml`. Bots import via that package, not `shared/`.
- **S-07 Audits Live in AUDIT/:** Automated reports and scans go under `AUDIT/<YYYYMMDD>_*`. They must not modify runtime code.
- **S-08 Init Hygiene:** Python packages must have `__init__.py`. No empty placeholder files beyond `__init__.py` and `.gitkeep`.
- **S-09 AUDIT Isolation**
  - CI must **ignore** all content inside `AUDIT/` (no linting, type checking, testing).
  - Runtime code must never import from `AUDIT/`.
  - Every audit under `AUDIT/<YYYYMMDD>_*` must add a pointer entry to `CHANGELOG.md`.

---

## **2) Coding & Behavior**

- **C-01 Async I/O:** Event handlers must not block. External calls (Sheets, HTTP) are async.
- **C-02 Logging:** Use structured logging helpers; no bare `print`. In addition to existing logging rules:
  - Logs must follow the humanized format used across Woadkeeper: `<emoji> <event> — <scope> • k=v • k=v [• details=…]`.
  - Names over IDs (IDs resolved only via cache).
  - Logging must never trigger external fetches.
  - Only log `reason=` on non-OK outcomes.
- **C-03 Imports:** Prefer absolute imports; no parent (`..`) imports.
- **C-04 Feature Flags:** Sourced from the **Features** sheet as `TRUE`/`FALSE` (case-insensitive normalization).
- **C-05 ENV Surface:** ENV is reserved for tokens, IDs, and infrastructure knobs. Feature flags **must not** live in ENV and are sourced exclusively from the FeatureToggles sheet.
- **C-06 Error Handling:** User-facing errors are friendly; exceptions are logged.
- **C-07 RBAC Centralized:** All role/permission checks use the standard RBAC helpers (no ad-hoc checks).
- **C-08 Single-Message Panels:** Interactive panels (like clan panels) update in place; avoid message spam.
- **C-09 No Legacy Paths:** No imports from removed legacy paths (e.g., top-level `recruitment/`, deprecated shared CoreOps shims, `shared/utils/coreops_*`).
- **C-10 Config Access:** Runtime config is accessed via the common config accessor (not scattered utility readers).
- **C-11 Forbidden Ports Import:** Import the runtime port helper from `shared.ports`. Using the old `shared.config` import for `get_port` fails guardrails (`scripts/ci/check_forbidden_imports.sh`, workflow `11-guardrails-suite`).
- **C-12 No Order Targets:** Onboarding rules and evaluators must reference question IDs (no order-number or sheet-position logic).
- **C-13 Render Free-Tier Constraints**
  - No continuous background polling.
  - Scheduled Sheets refreshes must be limited to **≤ 3/day** unless ADR-approved.
  - Health pings must be minimal (5–10 minutes).
  - External API calls should be batched where feasible.
- **C-14 Pagination Requirement** Any message or embed that exceeds Discord limits **must paginate**. Silent truncation is not allowed.
- **C-15 No Fallback Summaries** Welcome and Promo flows must use their designed summary embeds. Fallback summaries are allowed only if explicitly ADR-approved and must never expose internal debug information.
- **C-16 `!next` Behavior (NEW)** `!next` must list the next scheduled times for **all** registered modes/jobs. New scheduled jobs must automatically register with the unified scheduler so `!next` remains authoritative.
- **C-17 Standard Embed Colors**
  - Admin embeds: `#f200e5`
  - Recruitment embeds: `#1b8009`
  - Community embeds: `#3498db`
    New categories/colors require ADR approval.

---

## **3) Feature Toggles & Config Policy**

- **F-01 Sheet Source:** All feature toggles load from the `RECRUITMENT_SHEET › FeatureToggles` tab. No hard-coded flags or ENV overrides.
- **F-02 Defaults:** Each toggle has an explicit `TRUE` or `FALSE` default stored in the sheet. Missing entries are treated as `FALSE`.
- **F-03 Scope:** Toggles control runtime activation of recruitment modules and experiments. They do not alter infrastructure or cluster-wide settings.
- **F-04 Current Toggles:** The guardrail reads the `feature_name` column from the FeatureToggles worksheet via the same loader used at runtime (`modules.common.feature_flags.refresh` / `.values`). It then scans runtime code (excluding `AUDIT/` and tests) for usages of the central accessor `feature_flags.is_enabled("<toggle>")`. Any sheet-declared toggle not referenced through that accessor is reported here.
  - Current sheet entries include (non-exhaustive): `member_panel`, `recruiter_panel`, `clan_profile`, `recruitment_welcome`, `recruitment_reports`, `welcome_dialog`, `WELCOME_ENABLED`, `ENABLE_WELCOME_HOOK`, `PROMO_ENABLED`, `ENABLE_PROMO_HOOK`, `promo_dialog`, `FEATURE_RESERVATIONS`, `placement_target_select`, `placement_reservations`, `ClusterRoleMap`, `SERVER_MAP`, `housekeeping_enabled`, `mirralith_overview_enabled`, `ops_permissions_enabled`, `promo_watcher_enabled`, `resume_command_enabled`, `welcome_watcher_enabled`.
  - Resolution: remove unused rows from the sheet if a toggle is retired, or gate the relevant runtime path with `feature_flags.is_enabled("<feature_name>")` when the toggle should be enforced.
- **F-05 Additions:** New toggles must be added to the sheet and documented here with one-line purpose notes.
- **F-06 Runtime Behavior:** Toggles are evaluated dynamically at startup; no redeploy required solely for configuration updates.
- **F-07 Governance:** Repurposing or retiring a toggle requires ADR approval and removal in the next minor version.

---

## **4) Documentation**

- **D-01 Stable Titles:** No “Phase …” in any doc titles.
- **D-02 Footer Version (UPDATED):** Last line of every doc: `Doc last updated: YYYY-MM-DD (v0.9.8.x)` (updated from the older v0.9.x footer standard).
- **D-03 ENV SSoT parity:**
  - The **Environment variables** section in `docs/ops/Config.md` is the single source of truth for all environment variable keys.
  - `.env.example` **must** contain matching placeholders for every key listed in that ENV section so new deployments know which variables to set.
  - Other configuration described in `docs/ops/Config.md` — such as sheet tab names and sheet-based feature toggles (e.g., keys in the `Feature_Toggles` tab) — are **not** environment variables and must **not** be added to `.env.example`.
  - The ENV parity check script (`scripts/ci/check_env_parity.py`) is scoped to the ENV table/section only; sheet-only keys are out of scope for D-03.
- **D-04 Index:** `docs/README.md` lists and links every doc file under `docs/` with a 1-line blurb.
- **D-05 ADRs:** Architectural decisions are recorded as `docs/adr/ADR-XXXX.md` and linked where relevant.
- **D-06 Audit Discoverability:** Each audit adds files under `AUDIT/*` and a pointer in `CHANGELOG.md`.
- **D-07 Contract Priority:** CollaborationContract.md governs process and must link to this guardrails spec.
- **D-08 No Orphan Docs:** Every doc must be linked from `docs/README.md`.
- **D-09 Behaviour-Linked Tests:**
  Any PR that modifies functional behaviour in `modules/**`, `coreops/**`, or `shared/**`
  **must also update or add tests** in the matching test folder:

    • `tests/onboarding/**`
    • `tests/welcome/**`
    • `tests/recruitment/**`
    • `tests/placement/**`
    • `tests/coreops/**` or `tests/shared/**`
    • `tests/integration/**` (cross-module flows)
    • `tests/config/**` (config-loading behaviour)

  Exceptions are allowed only for docs-only, CI-only, or comment/typo fixes, and must be explicitly justified in the PR body. Silent omissions fail guardrails.
- **D-10 User-Facing Behaviour = Mandatory Doc Updates:**
  If a PR changes commands, help text, onboarding questions, summary formatting, watcher
  schedules, feature toggles, or any user-visible flow, the PR **must** update the relevant
  SSoT docs:
    • `docs/ops/CommandMatrix.md`
    • `docs/modules/<Module>.md`
    • `docs/ops/Config.md`
    • `docs/_meta/DocStyle.md` (if formatting changed)
    • `docs/Architecture.md` (if data flows changed)
    • `CHANGELOG.md`
  No new docs may be created unless an ADR authorises it.

---

## **5) Governance & Workflow**

- **G-01 Version Control:** Versions (bot, footers, changelog) change only on explicit instruction from the owner.
- **G-02 Codex Scope:** Codex performs only what the PR body instructs—no implicit deletions or moves.
- **G-03 PR Metadata:** PR bodies include the `[meta]...[/meta]` block for labels and milestone.
- **G-04 Docs Discipline:** Any code change that affects docs must update them in the same PR.
- **G-05 Audit-First:** Destructive refactors or removals require a prior audit that proves safety.
- **G-06 Naming:** Filenames are `lower_snake_case.md` (no spaces, no “Phase”).
- **G-07 CI Required:** Guardrail checks must be required status checks on PRs.
- **G-08 Secrets:** No secrets in repo or `.env.example`; use deployment envs.
- **G-09 PR Requirements — Tests/Docs Declaration** Every PR body must contain a section explicitly stating whether tests and docs were updated.
    One of the following formats is required:
    Tests:
    Updated: <file-path>
    Docs:
    Updated: <file-path>
      or (for allowed exceptions):
    Tests:
    Not required (reason: docs-only / CI-only / comment-only)
    Docs:
    Not required (reason: non-user-facing change)

### Guardrails CI automation

- The Repository Guardrails suite always runs to completion and writes both `guardrails_status` (`ok` / `fail`) and a markdown summary file.
- A follow-up CI step posts the summary comment to the PR using the generated markdown.
- A final CI step reads `guardrails_status` and fails the workflow only when it equals `fail`.
- This ordering guarantees the full report is published even when the workflow ends in failure.
- The PR “Guardrails Summary” comment now renders directly from `guardrails-results.json`, listing every automated guardrail code and status.
- The PR "Guardrails Summary" comment surfaces **every automated guardrail code** with an explicit status:
  - ✅ passed (no violations)
  - ❌ failed (one or more violations)
  - ⚪ skipped/not applicable (with a short reason)
  - Each code always appears, even when it passes or is skipped, to prevent silent drops.
  - The backing `guardrails-results.json` includes `code`, `description`, `status`, `violations`, and optional `reason` for every automated check result.
  - The suite writes these entries under a `results[]` array, and a legacy `checks{}` map mirrors the same codes for older consumers.

---

### Verification
Compliance script must check: structure (S), code (C), docs (D), governance (G), feature toggles (F) and write `AUDIT/<timestamp>_GUARDRAILS/report.md`.

Doc last updated: 2025-12-31 (v0.9.8.2)
