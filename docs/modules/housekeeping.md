# Housekeeping audit (roles and visitors)

This audit runs alongside the Daily Recruiter Update scheduler and focuses on
keeping member roles aligned with clan state while surfacing stalled Visitor
flows.

## Scope
- **Stray clan members.** Removes Raid when no clan tags remain and moves the
  member into Wandering Souls.
- **Half-fixed Wanderers.** Removes Raid from members who already carry
  Wandering Souls but have no clan tags.
- **Manual review.** Highlights Wandering Souls that still have clan tags
  attached.
- **Visitor health.** Buckets Visitors into: no ticket, closed-only tickets,
  and extra roles. No automatic role changes happen for Visitors.

## Inputs
- Role IDs: `RAID_ROLE_ID`, `WANDERING_SOULS_ROLE_ID`, `VISITOR_ROLE_ID`.
- Clan tags: `CLAN_ROLE_IDS` (comma-separated IDs of all clan roles).
- Ticket sources: `WELCOME_CHANNEL_ID`, `PROMO_CHANNEL_ID`.
- Report destination: `ADMIN_AUDIT_DEST_ID` (channel or thread ID).

## Output
Each run posts a single message to `ADMIN_AUDIT_DEST_ID` with numbered sections
for auto-fixes and Visitor buckets. Sections with no entries show `None`.

Doc last updated: 2025-12-04 (v0.9.8.2)
