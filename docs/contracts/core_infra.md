# Core Infra Contract

## Scope
Infra must provide reliable runtime, deployment, and observability surfaces while the bot guarantees readiness probes, watchdog
exits, and structured logging consistent with Phase 1 behavior.

## Inputs (Env / Secrets)
See [`docs/ops/Config.md`](../ops/Config.md#environment-keys) for full key definitions and defaults.
- `DISCORD_TOKEN` (required)
- `ADMIN_ROLE_IDS` (comma/space numeric)
- `STAFF_ROLE_IDS` (comma/space numeric)
- `WATCHDOG_CHECK_SEC` (prod default 360; non-prod 60)
- `WATCHDOG_STALL_SEC` (defaults to keepalive*3+30 if unset)
- `WATCHDOG_DISCONNECT_GRACE_SEC` (defaults to stall)
- `BOT_VERSION` (optional)
- `LOG_LEVEL` (optional)
- `PORT` (Render-provided)

## Sheets / Config
- Recruitment Sheet Config must expose `FEATURE_TOGGLES_TAB → FeatureToggles`.
- `FeatureToggles` worksheet schema: headers `feature_name`, `enabled` (case-insensitive).
- Only `TRUE` enables a feature; missing tabs/rows fail closed. See [README](../../README.md#feature-toggles)
  and [Ops Config](../ops/Config.md#feature-toggles-worksheet) for operator workflow.

## Intents / Permissions
- Dev Portal: enable **Server Members Intent** and **Message Content**.
- Code: `INTENTS.members=True`, `INTENTS.message_content=True`.

## Network / Health
- HTTP server (aiohttp) with:
  - `/ready`: returns 200 once server is up.
  - `/healthz`: returns 200 if heartbeat age <= stall; else 503.
- Single container instance; process exit triggers platform restart.

## Watchdog & Heartbeat
- Heartbeat source: gateway events (connect/ready/socket receive).
- Config:
  - check interval = `WATCHDOG_CHECK_SEC`
  - stall = `WATCHDOG_STALL_SEC` (or derived)
  - disconnect grace = `WATCHDOG_DISCONNECT_GRACE_SEC` (or stall)
- Exit conditions:
  - Connected but zombie (age > stall) and latency missing/poor → `exit(1)`
  - Disconnected for > disconnect grace → `exit(1)`

## Command Routing & Prefix
- Supported: `!ops`, `!ops␣`, `ops`, `ops␣`, `@mention`.
- Admin bang shortcuts: `!env`, `!reload`, `!health`, `!digest`, `!checksheet`, `!config`, `!help`, `!ping`, `!refresh all` (Admin role only).
- `!env` returns a four-page admin overview (Overview, Channels, Roles, Sheets & Config) and surfaces warnings for missing/not-found config on Page 1.
- No bare-word shortcuts.

## External Command Ownership
- `CommandNotFound` handling consults the cached shared HelpCommands registry to distinguish Woadkeeper commands from commands owned exclusively by external bots.
- Commands with only enabled, nonblank, non-Woadkeeper owners are ignored silently so another bot can handle them.
- Unknown commands, Woadkeeper-owned commands, mixed ownership, blank ownership, unavailable help data, lookup failures, and non-`CommandNotFound` exceptions continue through normal command-error reporting.
- Command ownership must remain sheet/cache-driven; do not add hard-coded external command lists in `app.py`.

## CoreOps v1.5 contract
- CoreOps integrates with the cache service exclusively through the public API surface:
  `list_buckets()`, `get_snapshot(name)`, `refresh_now(name, actor=…)`, and telemetry helpers.
- Private internals such as `_CONFIG_CACHE` or `_sheet_cache_snapshot` are considered
  implementation details and **must not** be imported or accessed directly.
- Commands that render operational embeds (`!ops health`, `!ops digest`, `!ops checksheet`)
  consume only public telemetry payloads.
- Guardrails:
  - No hard-coded IDs in commands or watchers; everything resolves through the config
    registry and cache metadata.
  - Manual refresh commands always include the invoking actor in the telemetry record.
  - Health/Digest/Checksheet embeds are validated in CI to ensure they do not reference
    private cache structures.
  - All external I/O must fail soft — return cached data when available and log the
    failure rather than crashing the cog.

## RBAC (Role-based)
- Staff gate = Admin role OR any Staff role.
- Admin-only actions (e.g., bang shortcuts).
- No user-ID gating.

## Logging
- Level via `LOG_LEVEL` (default INFO).
- Startup confirms parsed role IDs.
- Logs heartbeat/watchdog decisions and command errors.

## CI/CD
- GitHub Actions deploys queue sequentially; only one run per branch can execute at a time.
- When a newer push touches at least one of the same files, the older queued run is cancelled before build.
- Render deploy via hook; container builds with Dockerfile; Python 3.12.
- Example workflow log:
  ```
  2025-10-24T18:12:04Z queued → waiting for prior run
  2025-10-24T18:13:19Z cancelled → superseded by commit touching shared file
  2025-10-24T18:14:02Z started → latest commit entering deploy
  ```

## SLO-ish Expectations
- Ready within ~10s after container up.
- Auto-recover on gateway stalls/disconnects via watchdog exit+restart.
- Low chat noise; background-first.

## Change Management
- Backwards-compatible env keys; no behavior changes without CHANGELOG entry.
- Embed footer standardized: `Bot vX.Y.Z · CoreOps vA.B.C` (Discord timestamp replaces
  inline timezone text).

Doc last updated: 2025-12-03 (v0.9.8.2)
