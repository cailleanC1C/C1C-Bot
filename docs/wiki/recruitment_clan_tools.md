# Recruitment & Clan Tools

Recruitment reads normalized clan information, tags, criteria, availability, and configured assets into caches used by member search, recruiter match, clan profiles, open-spots updates, ads, welcomes, and reporting.

## Surfaces

- **Member search (`clansearch`)** filters eligible clans and pages results without exposing staff-only notes.
- **Recruiter match (`clanmatch`)** provides staff controls and ticket-aware matching.
- **Clan profile (`clan`)** presents the crest/profile and entry criteria; emoji/image generation follows configured size/proxy rules.
- **Open spots and reservations** share derived availability. Update through supported commands/workflows, not an ad hoc status cell.
- **Clan ads** publish configured content on schedule or via admin command.
- **Welcome** uses clan templates, role/channel routing, and general notices after placement.
- **Reports** produce the daily recruiter update and open-ticket visibility at configured destinations/times.

After clan data changes, validate headers with `!checksheet` and run `!refresh clansinfo`. If profile/search disagree, compare cache timestamps and availability derivation before editing data again.

Doc last updated: 2026-07-20 (v0.9.8.2)
