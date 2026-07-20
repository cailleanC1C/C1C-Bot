# Housekeeping & Maintenance

## Cleanup

Sheet-driven target rows define channel/thread, mode, minimum age, and dry-run. Pinned messages are never deleted. Start in dry-run, inspect candidate/skip/error totals, then enable deletion deliberately. `cleanup run` is the manual trigger; scheduled execution writes only bot-owned status columns.

## Keepalive

Housekeeping keepalive scans configured threads or parent channels and posts only after the configured stale interval. Its Sheet toggle, target tab, message, cadence, and stale threshold are required. This is distinct from the HTTP service keepalive controlled by deployment variables.

## Membership and role audits

- **Wandering Souls:** reports members whose Wandering, clan, Raid, and exclusion-role combinations disagree; `investigate` provides diagnostics.
- **Realmwalker audit:** checks configured Realmwalker eligibility/membership and routes the report to its audit destination.
- **Role audit:** scheduled/manual reporting reconciles safe Raid/Wandering cases and reports clan/visitor/ticket anomalies. Check role hierarchy before enabling corrective actions.

## Scheduled content

- **Mirralith overview:** renders configured overview content on schedule; `mirralith refresh` forces a refresh. Requires `mirralith_overview_enabled`, valid source, and destination.
- **C1C ad:** publishes configured community advertising on its schedule or through `c1cad`. Validate preview/content and destination first.
- **Guides help index:** assembles navigational help from configured guide posts. Use `guideshelpindex refresh` after guide/destination changes and check for inaccessible message/channel references.

## Achievements

- **Achievement images:** reads achievement definitions/assets and publishes or refreshes rendered boards with `achievements publish|refresh`. Validate attachment/embed permissions and image limits.
- **Achievement collector:** collects configured achievement claims/state and supports leaderboard preview, publish, and member rank. Confirm its toggle, source headers, schedule, and destination; preview before publish.

For all jobs, inspect scheduler registration with `!next`, health/retries with `!digest`, and use targeted refresh/retry rather than restarting repeatedly. Required Sheet configuration must fail closed without invented ENV fallbacks.

Doc last updated: 2026-07-20 (v0.9.8.2)
