# `!perm` Quickstart

The `!perm` command launches the interactive Permissions UI. It is the **only** permissions management path.

## Requirements
- Admin-only command.
- Caller must have **Manage Channels** or **Administrator**.
- Bot must have **Manage Channels**.
- Feature toggle `ops_permissions_enabled` must be ON.

If the toggle is OFF, the bot replies: `Permissions module is disabled.`

## Flow
1. Run `!perm` in a guild channel.
2. Use **Pick Role** to select the target role.
3. Use **Pick Targets** to select categories and channels.
   - Categories expand to all channel types under them.
   - Blacklists (`PERMS_BLACKLIST_CHANNEL_IDS`, `PERMS_BLACKLIST_CATEGORY_IDS`) are excluded.
4. Use **Pick Permissions** to mark Allow/Deny changes.
5. Preview the exact diff.
6. Click **CONFIRM APPLY** to execute.

## Apply Behavior
- Only Allow/Deny permissions are written.
- Unchanged permissions are preserved.
- Errors are isolated per channel.
- A summary is posted to the ops log channel when configured.

Doc last updated: 2025-12-31 (v0.9.8.2)
