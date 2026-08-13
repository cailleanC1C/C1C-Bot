# Live Arena Sheets Read Budget

## Summary

Live Arena organizer interactions and delayed historical refreshes now reuse identical Google Sheets reads within one logical operation instead of repeatedly consuming the per-user read quota.

## Changes

- Organizer transitions use one read scope for the mutation preflight/work and a fresh scope for post-write panel refreshes.
- Close Registration preflight reuses identical authorization/config/roster reads.
- Standalone public and organizer panel syncs use the shared per-operation read scope.
- Hall of Fame startup refresh is staggered away from the core Live Arena startup reconciliation and retries boundedly on Sheets quota errors.
- Read-budget logging reports physical reads and reused reads for Live Arena operations.

## Safety

- No Google Sheets schema, config, or data changes are made by this runtime hardening.
- Mutation and post-write refresh scopes remain separate so cached pre-write tournament state is never reused after a status change.
- Existing Sheet writes and competition truth remain unchanged.

---
C1C Bot audit record.
