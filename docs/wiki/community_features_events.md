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
## Leagues weekly history

Before publishing weekly league images, the bot captures config-driven cluster performance into append-only history. Four Leagues Config keys resolve the capture-spec, active-clan map, event-history, and evaluation tabs; capture specs resolve all source worksheets, ranges, and column letters. Active clans missing from a source are recorded as `missing`, while former/unmapped source clans are ignored. Blank, invalid, or zero weekly scores remain blank—not valid zeroes.

Capture validation occurs before any Discord export/post. Identical retries deduplicate, conflicting history aborts without overwrite, and negative cumulative win deltas abort. Discord failures do not roll back a successful capture, and the pipeline never clears Stormforged inputs. Current CvC/Siege data supports weekly score and result-only cumulative win/loss history, but not full cycle, participation, action, class, or mode-rating fields.

Doc last updated: 2026-08-03 (v0.9.8.2)
