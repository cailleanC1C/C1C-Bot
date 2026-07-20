# Discord Roles & Permissions

## Principles

Use least privilege, keep the bot role below only roles it must not manage, and configure IDs through documented ENV/Sheets sources. Do not hard-code IDs. Category inheritance is the default; add channel/thread overrides only when required.

## Common capability map

- Read/publish panels: View Channel, Send Messages, Embed Links, Attach Files, Read Message History.
- Ticket threads: View Channel, Send Messages in Threads, Manage Threads where watcher/close behavior requires it.
- Cleanup: Read Message History and Manage Messages in explicitly configured targets.
- Reactions: Add Reactions, Read Message History, and Manage Roles for reaction roles.
- Placement/audits: Manage Roles with the bot role above every role it may add/remove.
- Pins/maps: Manage Messages where pin replacement is enabled.

## Permissions UI

`!perm` is admin-only and applies role overwrites interactively. `PERMS_BLACKLIST_CHANNEL_IDS` and `PERMS_BLACKLIST_CATEGORY_IDS` are numeric ID lists preventing sensitive targets from appearing. A permission/scope/access-list change requires the agent registry and PR rationale/guardrail updates; routine documentation here does not grant access.

## Role-sensitive features

Recruiter/staff/admin access, clan roles, Raid, Wandering Souls and its exclude role, Visitor, Realmwalker, fusion opt-in, and reaction roles all depend on configured IDs/names and hierarchy. Diagnose “Missing Permissions” separately from “Missing Access”: the former is capability/hierarchy; the latter commonly means the bot cannot see the channel/thread or its parent.

Doc last updated: 2026-07-20 (v0.9.8.2)
