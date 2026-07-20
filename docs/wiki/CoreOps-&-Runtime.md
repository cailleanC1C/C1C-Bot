# CoreOps & Runtime

CoreOps is the shared operational command package in `packages/c1c-coreops`. It owns health, digest, checksheet, environment/config views, help metadata, cache refresh, and config reload. `modules/common/runtime.py` composes caches, schedulers, watchers, health/watchdog state, logging, and feature startup.

## Operator model

- Use the lowest access tier that can perform the action; member help must not expose privileged commands.
- Cache refreshes are registered by bucket and report age/duration/result. Prefer a targeted refresh.
- The scheduler registry is the source for recurring jobs and `!next`; duplicate jobs should not be started on reconnect.
- Watchdog settings define stalled/disconnected thresholds. Keepalive HTTP pings are separate from housekeeping thread keepalive.
- Config/env diagnostic output must be redacted. Logs resolve names from cache and avoid REST fetches in log paths.

## Startup success criteria

Correct environment and guild allow-list, Sheets linkage, cache preload, persistent views, ticket watchers, scheduled jobs, health route, watchdog, and log destination should all initialize. A feature with invalid required config should stay disabled and explain why rather than guess a default destination.

For incident actions, see [[Operations Runbook]] and [[Troubleshooting]].

Doc last updated: 2026-07-20 (v0.9.8.2)
