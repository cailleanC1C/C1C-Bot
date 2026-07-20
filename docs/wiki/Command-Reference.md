# Command Reference

Prefix examples use `!`; the configured prefix/mention route applies. “Admin” means Discord administrator or the CoreOps admin policy; “staff” means the configured recruiter/staff policy. Parent groups are included because they are active invocations. Aliases (`mercy`, `lego`, and `mythic primal`) are listed separately.

## CoreOps and administration

| Command | Access | Purpose | Owner module | Key gate/config |
|---|---|---|---|---|
| `ops` | User | CoreOps command/help entry point. | `c1c_coreops.cog` | CoreOps RBAC |
| `ops help [command]` | User | Accessible command index/details. | `c1c_coreops.cog` | command metadata |
| `ops ping` | User | Bot responsiveness check. | `c1c_coreops.cog` | none |
| `ops health`, `health` | Admin | Cache, gateway, scheduler, and watchdog health. | `c1c_coreops.cog` | health API/config |
| `ops checksheet [--debug]`, `checksheet` | Admin | Validate configured workbooks, tabs, and headers. | `c1c_coreops.cog` | sheet IDs and Config tabs |
| `ops digest`, `digest` | Staff / Admin | Operational status digest. | `c1c_coreops.cog` | cache/scheduler registry |
| `ops env [channels|roles|sheets|config]`, `env [overview|channels|roles|sheets|config]` | Admin | Redacted environment/config overview. | `c1c_coreops.cog` | ENV and registry |
| `ops config`, `config` | Admin | Show resolved runtime configuration. | `c1c_coreops.cog` | Config tabs |
| `cfg [KEY]` | Admin | Read a merged config value and source. | `modules.coreops.cmd_cfg` | Config tabs |
| `ops reload [--reboot]`, `reload [--reboot|onboarding]` | Admin | Reload registry/schema; optionally soft reboot. | `c1c_coreops.cog` | reload policy |
| `ops refresh [bucket]`, `refresh [bucket]` | Admin | Refresh a registered cache bucket. | `c1c_coreops.cog` | cache registry |
| `ops refresh all`, `refresh all` | Admin | Refresh all cache buckets. | `c1c_coreops.cog` | 30-second guild cooldown |
| `ops refresh clansinfo`, `refresh clansinfo` | Staff / Admin | Refresh clan roster information. | `c1c_coreops.cog` | recruitment workbook |
| `ops helpseed`, `helpseed` | Admin | Seed/export command help metadata. | `c1c_coreops.cog` | help metadata sheet/config |
| `ping` | Admin | Add a reaction for shard responsiveness. | `cogs.app_admin` | admin check |
| `servermap`, `servermap refresh` | Admin | Inspect/rebuild pinned server map output. | `cogs.app_admin` | server-map destinations/toggle |
| `guideshelpindex`, `guideshelpindex refresh` | Admin | Inspect/rebuild the guides help index. | `cogs.app_admin` | guides index config/toggle |
| `next` | Admin | Show upcoming scheduled jobs. | `cogs.app_admin` | scheduler registry |
| `whoweare` | Admin | Publish the current Who We Are role map. | `cogs.app_admin` | WhoWeAre tab/destination |
| `perm` | Admin | Open the Discord permissions editor. | `modules.ops.permissions_ui` | blacklist IDs; Discord permissions |

## Recruitment, onboarding, and placement

| Command | Access | Purpose | Owner module | Key gate/config |
|---|---|---|---|---|
| `welcome <clan> [member] [note]` | Staff | Post legacy clan welcome and notices. | `cogs.recruitment_welcome` | `recruitment_welcome`; templates/routing |
| `welcome-refresh` | Admin | Refresh welcome templates. | `cogs.recruitment_welcome` | WelcomeTemplates config |
| `clansearch` | User | Interactive member clan search. | `cogs.recruitment_member` | `member_panel` |
| `clan <tag>` | User | Clan profile and criteria card. | `cogs.recruitment_clan_profile` | `clan_profile` |
| `clanmatch` | Staff | Interactive recruiter matching panel. | `cogs.recruitment_recruiter` | `recruiter_panel` |
| `setopenspots` | Staff | Update a clan's advertised openings. | `cogs.recruitment_open_spots` | recruitment workbook/availability |
| `clanads`, `clanads post [tag]` | Admin | Preview/post configured clan ads. | `cogs.recruitment_clan_ads` | clan-ads toggle, tab, destination |
| `report [recruiters]` | Admin | Run the recruiter operational report. | `cogs.recruitment_reporting` | `recruitment_reports`; destination |
| `roleaudit` | Admin | Run recruitment role consistency audit. | `cogs.recruitment_reporting` | role IDs/audit destination |
| `onb resume @member` | Staff + Manage Threads | Resume an onboarding panel in its ticket. | `modules.onboarding.cmd_resume` | onboarding schema/session |
| `finishplacement` | Staff | Complete placement and ticket actions. | `modules.onboarding.cmd_finishplacement` | welcome/promo routing and roles |
| `ticketbackfill` | Admin | Backfill ticket tracking records. | `modules.onboarding.cmd_finishplacement` | ticket tabs/watchers |
| `reserve <clan>` | Staff | Reserve a seat from a ticket. | `modules.placement.reservations` | `feature_reservations` |
| `reservations` | Staff | View/manage reservation state. | `modules.placement.reservations` | reservation tab/config |

## Community and housekeeping

| Command | Access | Purpose | Owner module | Key gate/config |
|---|---|---|---|---|
| `shards [type]` | User | Open private shard/mercy tracker. | `modules.community.shard_tracker.cog` | shard tracker tabs/channel |
| `shards set <type> <count>` | User | Set shard stash count. | same | allowed channel |
| `shards reminder-debug` | Admin | Diagnose shard reminder scheduling. | same | reminder config |
| `mercy [type]`, `mercy set …`, `lego`, `mythic primal` | User | Shard tracker compatibility aliases. | same | shard tracker config |
| `fusion`, `titan` | User | Open event/tournament progress surfaces. | `modules.community.fusion.cog` | fusion/titan toggles and tabs |
| `fusion debug` | Admin | Diagnose cached fusion events. | same | fusion cache |
| `fusion refresh-announcement` | Admin | Refresh the configured announcement. | same | announcement destination |
| `fusion publish`, `titan publish` | Admin | Publish configured event announcement. | same | destination/config tabs |
| `leagues`, `leagues post` | Admin | Inspect/manually run league autoposting. | `modules.community.leagues.cog` | leagues schedule/destinations |
| `progressguides`, `progressguides publish`, `progressguides refresh` | Admin | Maintain progress-guide posts. | `modules.community.progress_guides.cog` | guide tab/destinations |
| `reactrole` | Admin | Publish/configure reaction-role message. | `modules.community.reaction_roles` | reaction-role tab/destination |
| `cleanup`, `cleanup run` | Admin | Inspect/run housekeeping cleanup. | `modules.housekeeping.cleanup` | cleanup toggle/tab/dry-run |
| `achievements`, `achievements publish`, `achievements refresh` | Admin | Publish/refresh achievement images. | `cogs.housekeeping_achievements` | achievements workbook/destination |
| `achievementcollector [preview|publish|rank]` | User; publish admin | View/publish leaderboard or a member rank. | `cogs.housekeeping_achievement_collector` | collector toggle/tab/destination |
| `wanderingsouls`, `wanderingsouls investigate` | Admin | Report or investigate wandering-role anomalies. | `cogs.housekeeping_wandering_souls` | wandering/exclusion role IDs |
| `audit`, `audit realmwalker` | Admin | Run Realmwalker membership audit. | `cogs.housekeeping_realmwalker` | Realmwalker role/config |
| `mirralith`, `mirralith refresh` | Admin | Inspect/refresh Mirralith overview. | `cogs.housekeeping_mirralith` | `mirralith_overview_enabled` |
| `c1cad` | Admin | Publish the configured C1C advertisement. | `cogs.housekeeping_c1c_ad` | C1C ad tab/destination |
| `clanrole`, `clanrole remove @member` | Staff | Remove clan role and apply related cleanup. | `cogs.clanrole_management` | clan/Raid/Wandering role config |

Scheduled-only features such as reset reminders, keepalive, achievement collection refresh, and role audit are documented in [[Feature Index]] and [[Housekeeping & Maintenance]] rather than invented as commands.

Doc last updated: 2026-07-20 (v0.9.8.2)
