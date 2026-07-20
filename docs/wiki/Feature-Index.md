# Feature Index

| Feature | What administrators should know | Primary owner | Main dependency |
|---|---|---|---|
| Welcome | Watches welcome tickets, renders question panels, records answers, places recruits, and sends clan welcomes. | `modules/onboarding`, `modules/recruitment/welcome.py` | onboarding Config/questions/templates and destinations |
| Promo | Watches promotion tickets and runs the promo-specific onboarding controller. | `modules/onboarding/watcher_promo.py` | promo ticket/config tabs and role routing |
| Recruitment panels | Member and recruiter search panels with filtering and paging. | `modules/recruitment/views` | `member_panel`, `recruiter_panel` |
| Clan search | Matches member criteria to available clans. | `modules/recruitment/search.py` | ClanInfo/availability data |
| Clan profiles | Profile/criteria cards, crest pipeline, reaction flip. | `modules/recruitment/clan_profile.py` | `clan_profile`, clan/profile tabs |
| Clan ads | Scheduled/manual clan advertisement publication. | `modules/recruitment/clan_ads.py` | toggle, schedule, ad tab, destination |
| Reservations | Ticket-scoped seat reservations and expiry jobs. | `modules/placement/reservations.py` | `feature_reservations`, reservation schema |
| Shards | Private shard stash, pulls, mercy, and reminders. | `modules/community/shard_tracker` | tracker tabs, channel, reminder schedule |
| Fusion / Titan | Opt-in progress, announcements, reminders, and cleanup. | `modules/community/fusion` | event tabs, toggles, roles/destinations |
| Reset reminders | Persistent reminder views plus scheduled reconciliation. | `modules/community/reset_reminders` | reminder workbook/tabs and destinations |
| Progress guides | Publishes and refreshes guide posts. | `modules/community/progress_guides` | guide definitions/destinations |
| Leagues | Scheduled C1C league reminders/autoposts. | `modules/community/leagues` | league schedule/destination config |
| Achievements | Renders and publishes achievement image boards. | `modules/housekeeping/achievements.py` | achievements workbook and image config |
| Achievement collector | Collects claims and publishes/ranks a leaderboard. | `modules/housekeeping/achievement_collector.py` | collector tab/toggle/destination |
| Server map | Builds pinned navigation from live Discord structure. | `modules/ops/server_map.py` | destination/category exclusions |
| Who We Are | Builds role-holder overview from configured definitions and live roles. | `cogs/app_admin.py` | WhoWeAre tab and destination |
| Permissions UI | Applies channel/category role overwrites interactively. | `modules/ops/permissions_ui.py` | blacklist IDs and Discord Manage permissions |
| Cleanup | Safely deletes eligible old messages using per-target policies. | `modules/housekeeping/cleanup.py` | sheet-only toggle/tab, dry-run, cadence |
| Keepalive | Prevents configured threads from auto-archiving. | `modules/housekeeping/keepalive.py` | sheet-only target tab and cadence |
| Wandering Souls | Diagnoses role/clan mismatches. | `modules/housekeeping/wandering_souls.py` | Wandering/exclusion/clan role IDs |
| Realmwalker audit | Audits Realmwalker eligibility/membership. | `modules/housekeeping/realmwalker.py` | role and audit routing config |
| Mirralith overview | Scheduled/manual overview refresh. | `modules/housekeeping/mirralith_overview.py` | enable toggle, source, destination |
| C1C ad | Scheduled/manual community ad publication. | `modules/housekeeping/c1c_ad.py` | ad content, schedule, destination |
| Role audit | Reconciles/reports Raid, clan, Wandering, and visitor anomalies. | `modules/housekeeping/role_audit.py` | role IDs, ticket channels, audit destination |
| Guides help index | Builds help navigation for guide posts. | `modules/housekeeping/guides_help_index.py` | guide/index destinations and schedule |
| Recruitment reports | Daily recruiter update and open-ticket reporting. | `modules/recruitment/reporting` | `recruitment_reports`, time/destinations |
| Placement | Finishes onboarding placement, roles, notices, and ticket state. | `modules/onboarding/cmd_finishplacement.py` | routing, role, ticket, and schema config |
| Reaction roles | Maintains configured reaction-to-role messages. | `modules/community/reaction_roles.py` | reaction-role definitions and bot permissions |
| CoreOps | Health, configuration, checksheet, refresh/reload, help, and digest. | `packages/c1c-coreops` | ENV, Config tabs, caches, RBAC |

See [[Command Reference]] for invocations and [[Sheets & Config Reference]] for source precedence.

Doc last updated: 2026-07-20 (v0.9.8.2)
