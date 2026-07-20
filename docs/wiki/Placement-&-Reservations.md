# Placement & Reservations

Reservations hold a clan seat for a recruit inside an eligible ticket and feed the same availability derivation used by recruitment results. Placement then applies configured clan/Raid roles, notices, ticket state, and reservation completion/release.

## Reservation lifecycle

1. Staff selects a valid clan with `!reserve <clan>` in the scoped ticket.
2. The service validates feature gate, ticket/recruit identity, clan capacity, and existing active reservations.
3. It records reservation ownership and timestamps using header-resolved schema, then recomputes availability.
4. Scheduled jobs expire stale reservations; completion consumes/releases the reservation as defined by placement state.

Use `!reservations` for the supported management view. Do not manually create competing rows or reorder/remove required headers. Diagnose mismatches by checking reservation state, source clan capacity, derived availability, ticket identity, job health, and cache age in that order.

Placement actions require Discord role hierarchy and ticket permissions. They should be retry-safe; confirm current roles, notices, and persisted state before rerunning `!finishplacement`.

Doc last updated: 2026-07-20 (v0.9.8.2)
