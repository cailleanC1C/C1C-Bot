# Sheets read broker

`shared.sheets.read_broker` is the process-wide coordination layer intended to become the only physical Google Sheets read boundary in C1C-Bot.

## Current migration status

The shared **async** Sheets facade in `shared.sheets.async_core` is now brokered. Runtime callers using `afetch_records`, `afetch_values`, or `asheets_read` therefore share one process-wide quota pacer and single-flight map instead of independently retrying physical reads.

The remaining synchronous `shared.sheets.core` read paths, import-time Sheet-backed config hydration, startup warm-up orchestration, write invalidation wiring, and direct-read CI enforcement are intentionally follow-up work. Do not assume those paths are brokered yet.

## Terms

- **Logical read:** one feature or scheduler request for Sheet-backed data.
- **Physical read:** one Google API request. Retry attempts count as physical reads because they consume quota too.
- **Canonical key:** the underlying resource (`sheet_id + operation + worksheet/range`). Caller/module names never participate in cache identity.

## Broker responsibilities

The broker provides process-wide single-flight request coalescing, result caching, stale-while-revalidate, prioritized request pacing, centralized 429/`RESOURCE_EXHAUSTED` retry/backoff, cache invalidation, and physical-read telemetry.

Named policies are centralized: `STATIC_CONFIG`, `RUNTIME_CONFIG`, `ACTIVE_STATE`, `BACKGROUND_DATA`, and `FRESH_REQUIRED`.

`SHEETS_READ_BUDGET_RPM` controls the process read budget when set. C1C-Bot currently defaults to `32`, but production should set it explicitly so the account-wide allocation across bots is visible in deployment configuration.

## Async boundary and freshness

Existing async feature/state reads default to `FRESH_REQUIRED`. This is deliberate: moving a caller behind the broker must not silently turn a formerly live state read into a multi-minute cache. Concurrent identical requests can still coalesce, but sequential state reads remain fresh unless the caller explicitly chooses another policy.

Known safe configuration resources may opt into longer policies. The canonical Milestones `Config` tab uses `STATIC_CONFIG` at `HIGH` priority, giving it a 30-minute fresh window and a 12-hour stale-safe window. Reset Reminders, Fusion, and other async consumers resolving Milestones tab names therefore share the same Config snapshot instead of independently reading the Config tab.

A stale-but-usable Config entry is returned immediately while one coalesced refresh runs. If Google is temporarily quota-limited, callers can continue using the last safe Config snapshot within the stale window.

The resolved operational tab itself is not automatically treated as static. For example, resolving `RESET_REMINDER_TAB` can use stale-safe Config, while reading the actual Reset Reminder rows still defaults to `FRESH_REQUIRED`.

## Retry ownership

For brokered async read operations, the broker is the only quota retry/backoff owner. The adapter loader invoked by `async_core` performs one physical attempt. This prevents retry multiplication such as:

```text
broker retry
× core retry
× feature cache retry
```

The legacy cache service may retain its historical retry for non-rate-limit loader failures, but it must not retry a 429/`RESOURCE_EXHAUSTED` after the broker has exhausted its own policy. `refresh_now()` also joins an already-running bucket refresh so a cold `get()` followed by `refresh_now()` cannot start duplicate physical loads.

Generic `acall_with_backoff` / `a_to_thread_with_backoff` remain migration/write compatibility helpers; they are not the preferred read path.

## Invalidation

The broker can invalidate one exact resource, one worksheet, or an entire workbook. Generation tracking prevents an older in-flight read from repopulating a cache entry after invalidation. Automatic write-to-read invalidation wiring remains follow-up work.

## Remaining migration rule

The target rule remains:

> Feature code must not directly perform physical Google Sheets reads. All read-side gspread calls must pass through the shared broker/gateway.

The next migration phase will remove remaining synchronous/import-time physical reads, move startup hydration into the planned priority-based warm-up window, add scheduler snapshot semantics/diagnostics, wire write invalidation, and add CI enforcement against new direct reads.
