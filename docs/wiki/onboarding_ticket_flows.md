# Onboarding & Ticket Flows

## Welcome flow

The welcome watcher identifies configured ticket threads, establishes membership, loads the current question schema, renders the interactive panel, persists a resumable session, evaluates placement rules, writes the approved summary, and hands off placement/welcome actions. Summary detection and dedupe prevent duplicate completion. `!onb resume` restores a valid session; `!finishplacement` performs staff completion.

## Promo flow

The promo watcher uses the same shared session/rendering infrastructure but its own controller, ticket routing, rules, and logging. Do not treat welcome and promo destinations or schemas as interchangeable.

## Operational rules

- Ticket channel/parent and external Ticket Tool behavior are configured; private thread access and bot membership are prerequisites.
- Questions and routing come from Config/feature tabs. Reload onboarding after schema edits and verify the schema hash.
- Async handlers must use async Sheets helpers. Avoid duplicate writes/actions when Discord retries interactions.
- Persistent views must register on startup. Stale/invalid sessions should fail visibly and be resumed or restarted deliberately.
- Use `ticketbackfill` only for bounded administrative repair after confirming watcher scope.

See [[Placement & Reservations]] for seat handoff and [[Discord Roles & Permissions]] for required thread permissions.

Doc last updated: 2026-07-20 (v0.9.8.2)
