# Permissions UI

The Permissions UI replaces the legacy allow/deny list workflow. Use `!perm` to open an interactive builder that applies role overwrites directly to selected channels.

## Entry Points
- Command: `!perm`
- Module: `modules/ops/permissions_ui.py`
- Feature toggle: `ops_permissions_enabled`
  - OFF → replies `Permissions module is disabled.`
  - ON → launches the UI

## Access & Safety
- Admin-only command (`admin_only` guard).
- Caller must have **Manage Channels** or **Administrator**.
- Bot must have **Manage Channels**.
- Blacklists:
  - `PERMS_BLACKLIST_CHANNEL_IDS`
  - `PERMS_BLACKLIST_CATEGORY_IDS`

## UI Flow

### 1) Builder
Buttons:
- Pick Role
- Pick Targets
- Pick Permissions
- Preview
- Apply (disabled until preview)
- Cancel

Live summary includes:
- Selected role
- Selected categories + channels
- Expanded channel count
- Excluded by blacklist
- Excluded (bot lacks permission)
- Selected permission changes (Allow/Deny only)

### 2) Pick Role
Dropdown select from guild roles.

### 3) Pick Targets
- Multi-select categories and channels.
- Category selection expands to **all channel types** under the category.
- Blacklist exclusions applied.
- Channels where overwrites cannot be edited are excluded.

### 4) Pick Permissions (Pattern A)
- Paged permissions list (10–15 per page).
- Each permission cycles: **Unchanged → Allow → Deny → Unchanged**.
- Controls: Prev / Next / Done / Clear / Back.

### 5) Preview
- Role
- Expanded channel count
- Excluded counts
- Exact diff (Allow/Deny only)
- Buttons: **CONFIRM APPLY**, Back, Cancel.

### 6) Apply
- Explicit confirmation required.
- Applies only Allow/Deny selections; all other permissions remain unchanged.
- Per-channel isolation with batching delays.
- Summary: updated, skipped, errors, duration.
- Log message posted to the ops log channel when configured.

## Apply Logic
For each eligible channel:
1. Read the existing role overwrite.
2. Apply only permissions marked Allow or Deny.
3. Leave all other permissions untouched.
4. Write the overwrite back.

Errors are isolated per channel. The run continues and reports aggregate results.

Doc last updated: 2025-12-31 (v0.9.8.2)
