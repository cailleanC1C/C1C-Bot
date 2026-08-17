# Sheets read broker foundation

`shared.sheets.read_broker` is the process-wide coordination layer intended to become the only physical Google Sheets read boundary in C1C-Bot.

This foundation PR does **not** migrate existing callers. Existing `shared.sheets.core`, `async_core`, cache buckets, feature readers, and startup flows remain unchanged until the follow-up migration PR.

## Terms

- **Logical read:** one feature or scheduler request for Sheet-backed data.
- **Physical read:** one Google API request. Retry attempts count as physical reads because they consume quota too.
- **Canonical key:** the underlying resource (`sheet_id + operation + worksheet/range`). Caller/module names never participate in cache identity.

## Broker responsibilities

The broker provides process-wide single-flight request coalescing, result caching, stale-while-revalidate, prioritized request pacing, centralized 429/`RESOURCE_EXHAUSTED` retry/backoff, cache invalidation, and physical-read telemetry.

Named policies are centralized: `STATIC_CONFIG`, `RUNTIME_CONFIG`, `ACTIVE_STATE`, `BACKGROUND_DATA`, and `FRESH_REQUIRED`.

`SHEETS_READ_BUDGET_RPM` controls the process read budget when set. The foundation has a safe default so tests and development do not require the environment variable.

## Stale behaviour

A fresh entry is returned immediately. A stale-but-usable entry is returned immediately while one coalesced background refresh runs. Data beyond its stale allowance waits for a new physical read. `FRESH_REQUIRED` never returns a cached stale value.

## Retry ownership

The broker owns read-side quota retry/backoff once callers are migrated. During this foundation PR the existing `shared.sheets.core` retry functions are intentionally unchanged and are **not** wrapped by the broker, preventing nested retry loops before the migration boundary is moved.

## Invalidation

Future write paths can invalidate one exact resource, one worksheet, or an entire workbook. Generation tracking prevents an older in-flight read from repopulating a cache entry after invalidation.

## Migration rule

Once migration is complete, feature code must not directly perform physical Google Sheets reads. All read-side gspread calls must pass through the shared broker/gateway. CI enforcement will be added in a follow-up PR after the existing callers have been migrated.
