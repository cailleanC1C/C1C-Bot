# Operations Runbook

## Deploy or restart

1. Confirm CI passes and the intended commit is on `main`; do not deploy a dirty/local tree.
2. Deploy through Render using the correct `prod` or `test` environment group.
3. Watch startup for config linkage, guild allow-list, cache preload, scheduler registration, watcher readiness, and watchdog lines.
4. Run `@Bot ping` (user route), then admin `!health` and `!digest`. Verify one low-risk feature in the target guild.
5. Roll back to the last known-good commit if imports/startup or core configuration fails; do not patch live Sheets to hide code failures.

## After Sheet changes

- Run `!checksheet` (or approved `--debug`) for schema/linkage.
- Refresh only the affected cache: `!refresh <bucket>`; use `!refresh all` when multiple dependent buckets changed.
- On onboarding question/schema changes, use `!reload onboarding` and verify the logged schema hash before resuming a ticket.
- Verify the relevant preview/dry-run and destination. A sheet edit does not normally require a process restart.

## After ENV changes

ENV is loaded at process startup. Save the Render environment change, restart/redeploy, then verify the redacted `!env`, `!config`, `!health`, startup logs, and feature destination. Never paste secret values into Discord.

## Health checks

Use `!health` for gateway/watchdog/cache status, `!digest` for cache age/jobs/retries, `!next` for upcoming jobs, and the public health endpoint/watchdog logs for platform reachability. Check Discord connectivity, scheduler progress, cache ages, Sheets latency, and the configured log channel together.

## Refresh versus reload

- **Refresh** re-reads one/all cache buckets without reconstructing the whole config registry.
- **Reload** rebuilds configuration; `reload onboarding` specifically reloads the question schema.
- **Restart/reboot** replaces process state and is appropriate after ENV/dependency/deploy changes or unrecoverable task state—not routine Sheet edits.

## CI failures

Open the first failing job, reproduce its exact command locally, and fix the cause rather than rerunning blindly. For guardrails, inspect repository policy and changed-file scope. For wiki publishing, run `python .github/scripts/validate_wiki.py`; link targets must match the mapped publish filename after spaces become hyphens. A publish clone/push failure can also mean the Wiki has not been initialized or authentication is insufficient. The workflow prefers the `WIKI_PUSH_TOKEN` secret: use a fine-grained personal access token scoped to the `C1C-Bot` repository with **Contents: Read and write**. When that secret is absent, the workflow uses `GITHUB_TOKEN`, for which repository Actions settings must grant **Read and write permissions**; the workflow also declares `contents: write`.

## Discord permission failures

Identify the bot operation, guild, category/channel/thread, and required permission. Check role hierarchy and category inheritance before adding overwrites. Typical needs include View Channel, Send Messages, Send Messages in Threads, Read Message History, Manage Threads/Messages/Roles, Embed Links, Attach Files, and Add Reactions. Keep least privilege; respect permissions UI blacklists. Retry a single safe action after correction and inspect the humanized error log.

## Sheets quota/rate-limit triage

Stop repeated manual refreshes. Confirm whether failures are 429/quota, timeout, auth, or schema errors; inspect backoff/retry and cache age. Let exponential backoff complete, coalesce requests through caches/async helpers, and refresh only the affected bucket after recovery. If persistent, check Google service-account/project quotas and sharing, then reduce scheduler/manual contention. Never bypass async Sheets helpers from a Discord handler.

Doc last updated: 2026-07-20 (v0.9.8.2)
