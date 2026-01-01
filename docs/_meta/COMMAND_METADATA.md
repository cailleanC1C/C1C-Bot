# Command Metadata Audit

## Summary
- Total commands discovered: 36
- Commands missing access_tier: 0
- Commands missing function_group: 0
- Commands missing both: 0

## Findings
| Command | File:Line | access_tier | function_group | Notes |
| --- | --- | --- | --- | --- |
| ops | packages/c1c-coreops/src/c1c_coreops/cog.py:1089 | user | operational | |
| ops health | packages/c1c-coreops/src/c1c_coreops/cog.py:1346 | admin | operational | |
| health | packages/c1c-coreops/src/c1c_coreops/cog.py:1352 | admin | operational | |
| ops checksheet | packages/c1c-coreops/src/c1c_coreops/cog.py:1913 | admin | operational | |
| checksheet | packages/c1c-coreops/src/c1c_coreops/cog.py:1920 | admin | operational | |
| ops digest | packages/c1c-coreops/src/c1c_coreops/cog.py:2036 | staff | operational | |
| digest | packages/c1c-coreops/src/c1c_coreops/cog.py:2042 | admin | operational | |
| ops env | packages/c1c-coreops/src/c1c_coreops/cog.py:2087 | admin | operational | |
| env | packages/c1c-coreops/src/c1c_coreops/cog.py:2094 | admin | operational | |
| ops help | packages/c1c-coreops/src/c1c_coreops/cog.py:2101 | user | operational | |
| ops ping | packages/c1c-coreops/src/c1c_coreops/cog.py:2113 | user | operational | |
| ops config | packages/c1c-coreops/src/c1c_coreops/cog.py:2316 | admin | operational | |
| config | packages/c1c-coreops/src/c1c_coreops/cog.py:2323 | admin | operational | |
| reload | packages/c1c-coreops/src/c1c_coreops/cog.py:2343 | admin | operational | |
| ops reload | packages/c1c-coreops/src/c1c_coreops/cog.py:2358 | admin | operational | |
| refresh | packages/c1c-coreops/src/c1c_coreops/cog.py:2372 | admin | operational | |
| ops refresh | packages/c1c-coreops/src/c1c_coreops/cog.py:2387 | admin | operational | |
| refresh all | packages/c1c-coreops/src/c1c_coreops/cog.py:2454 | admin | operational | |
| ops refresh all | packages/c1c-coreops/src/c1c_coreops/cog.py:2465 | admin | operational | |
| refresh clansinfo | packages/c1c-coreops/src/c1c_coreops/cog.py:2576 | admin | operational | |
| ops refresh clansinfo | packages/c1c-coreops/src/c1c_coreops/cog.py:2586 | staff | operational | |
| clan | cogs/recruitment_clan_profile.py:65 | user | recruitment | |
| clansearch | cogs/recruitment_member.py:20 | user | recruitment | |
| clanmatch | cogs/recruitment_recruiter.py:214 | staff | recruitment | |
| shards | modules/community/shard_tracker/cog.py:123 | user | milestones | |
| shards set | modules/community/shard_tracker/cog.py:137 | user | milestones | |
| mercy | modules/community/shard_tracker/cog.py:153 | user | milestones | |
| mercy set | modules/community/shard_tracker/cog.py:167 | user | milestones | |
| lego | modules/community/shard_tracker/cog.py:183 | user | milestones | |
| mythic primal | modules/community/shard_tracker/cog.py:215 | user | milestones | |
| ping | cogs/app_admin.py:23 | admin | operational | |
| report | cogs/recruitment_reporting.py:27 | admin | operational | |
| welcome | cogs/recruitment_welcome.py:41 | staff | recruitment | |
| welcome-refresh | cogs/recruitment_welcome.py:56 | admin | operational | |
| perm | modules/ops/permissions_ui.py:904 | admin | operational | |
| reload | packages/c1c-coreops/src/c1c_coreops/commands/reload.py:64 | admin | operational | |

## Hotspots
### Files
- None — coverage is complete.

### Directories
- None — coverage is complete.

## Suggested fixes
- Ensure new commands and aliases include `help_metadata(...)` coverage so the help surface stays descriptive.

Doc last updated: 2025-12-31 (v0.9.8.2)
