# Community Features & Events

## Shards and mercy

The shard tracker creates a private owner-only experience for stash counts, pull history, mercy progress, and reminders. `shards set` is channel-restricted; compatibility aliases (`mercy`, `lego`, `mythic primal`) use the same state. Diagnose reminder issues with the admin debug route and scheduler/cache status.

## Fusion and Titan

Event tabs drive announcements, opt-in progress sharing, preparation choices, reminders, and role cleanup. Reconciliation jobs should be idempotent. Preview/debug cached events before `fusion publish` or `titan publish`; validate destination and event dates/timezone.

## Reset reminders

Persistent views let members opt into configured reset reminders. Startup and scheduled reconciliation recreate missing jobs without duplicating them. Check reminder definitions, destination/timezone, persistent-view registration, and scheduler registry after changes.

## Progress guides and help index

Progress guides publish/refresh configured guide messages. The guides help index is the navigation layer over those posts. Refresh guides first, then the index, and ensure the bot can read target messages and post embeds.

## Leagues, achievements, and reaction roles

Leagues schedules weekly category posts/announcements and offers an admin manual run. Achievement boards/collector are described in [[Housekeeping & Maintenance]]. Reaction roles map configured message reactions to roles and require a valid role hierarchy plus message/reaction permissions.

Doc last updated: 2026-07-20 (v0.9.8.2)
