# Housekeeping Role & Visitor Audit Investigation

## Scope

Investigated the current implementation of the daily Role & Visitor Audit / housekeeping report, why Discord members with no roles or no meaningful roles are not appearing in that report, and all currently discoverable code paths that can remove Discord roles from members. This investigation is documentation-only and does not change runtime behavior or sheet schema.

## Current Daily Report Behavior

The daily Role & Visitor Audit is run as part of the Daily Recruiter Update bundle. `scheduler_daily_recruiter_update()` calls `run_full_recruiter_reports(bot, actor="scheduled")`, which posts the recruiter report, then calls `run_role_and_visitor_audit(..., dry_run=True)`, then posts the open-ticket report.

The audit resolves these required inputs before scanning: `RAID_ROLE_ID`, `WANDERING_SOULS_ROLE_ID`, `VISITOR_ROLE_ID`, `CLAN_ROLE_IDS`, an audit destination (`ADMIN_AUDIT_DEST_ID`, falling back to log channel IDs), and at least one ticket channel (`WELCOME_CHANNEL_ID` or `PROMO_CHANNEL_ID`). It scans allowed guilds from `GUILD_IDS` when configured, otherwise all bot guilds.

For each guild, `_audit_guild()` fetches all members, fetches welcome/promo ticket threads, builds a member-to-ticket map, and evaluates each member for these buckets:

1. **Stray members**: members with Raid, no configured clan role, and no Wandering Souls role. In scheduled/dry-run mode the report says it would remove Raid and add Wandering Souls; it does not actually mutate roles.
2. **Existing Wanderers that still have Raid but no clan role**: members with Raid and Wandering Souls, but no configured clan role. In scheduled/dry-run mode the report says it would remove Raid and keep Wandering Souls.
3. **Manual review: Wandering Souls with clan tags**: members with Wandering Souls and at least one configured clan role.
4. **Visitors without any ticket**: members with the configured Visitor role and no fetched ticket thread membership.
5. **Visitors with only closed tickets**: members with the Visitor role whose fetched tickets are all closed/archived.
6. **Visitors with extra roles**: members with the Visitor role plus any role other than Visitor and `@everyone`.

The report renderer only adds sections when a bucket has at least one line. If no detected sections or action sections exist, the embed description is empty and only the footer shows the checked member count.

## Roleless / No-Role Member Check

* **Does the current audit check for members who only have `@everyone`?** No. The audit scans all fetched members, but there is no classification or report bucket for members whose role set is only the guild `@everyone` role.
* **Does it check for members with no clan tag role, no visitor role, no raid role, no Wandering Souls role, and no other configured allowed role?** No. There is no general "no meaningful roles" or "allowed role" concept in the current audit. The only configured role sets used by the audit are Raid, Wandering Souls, Visitor, and configured clan roles.
* **What exact criteria does the code currently use?** `_classify_roles()` computes only three meaningful non-ok role states:
  * `stray`: has Raid, does not have any configured clan role, and does not have Wandering Souls.
  * `drop_raid`: has Raid and Wandering Souls, but does not have any configured clan role.
  * `wander_with_clan`: has Wandering Souls and at least one configured clan role.
  * Everything else is `ok`.

  Visitor checks only run when the member has the configured Visitor role. A Visitor's "extra roles" are every role except Visitor and `@everyone`.
* **If roleless members are not checked, say that clearly.** Roleless/no-role members are not checked by the current daily Role & Visitor Audit.

## Why Roleless Members Are Missing From The Report

* **Are they filtered out?** They are not filtered out before the scan; they are included in the fetched member list and counted in the footer's checked-member total. However, the classification logic returns `ok` for roleless/no-role members because they do not have Raid, Wandering Souls, a configured clan role, or Visitor.
* **Is the report section missing?** Yes. There is no report section for "roleless", "only @everyone", "no meaningful roles", or "no configured allowed roles" members.
* **Is the audit only checking bad combinations of known roles?** Yes. The role audit checks known problematic combinations involving Raid, Wandering Souls, configured clan roles, and Visitor/ticket state. It does not do a broad membership hygiene check for absence of roles.
* **Is the report capped/truncated?** The current renderer does not implement an explicit per-section cap/truncation. Discord embed description length still applies at send time, so a very large report could fail to send, but roleless members are missing because they are never collected into a report bucket, not because a cap hides them.
* **Is there a difference in code between "no roles" and "no clan roles"?** Yes. "No clan roles" means the member lacks any role ID from `CLAN_ROLE_IDS`; it is only meaningful to the current role-classification logic when combined with Raid and/or Wandering Souls. "No roles" / "only @everyone" has no separate logic and falls through as `ok`.

## Relevant Files And Functions

* **Scheduled housekeeping / daily job**
  * `modules/recruitment/reporting/daily_recruiter_update.py`
    * `_scheduled_time()` reads `REPORT_DAILY_POST_TIME`.
    * `scheduler_daily_recruiter_update()` is the daily task loop entrypoint.
    * `ensure_scheduler_started()` starts/cancels the daily task based on the `recruitment_reports` feature toggle and report destination.
    * `run_full_recruiter_reports()` runs the recruiter report, role/visitor audit, and open-ticket report.
  * `modules/common/runtime.py`
    * Startup/scheduler registration imports housekeeping modules and schedules cleanup/keepalive jobs under the `housekeeping_enabled` toggle. The Role & Visitor Audit itself is coupled to the recruitment daily report scheduler, not the cleanup watcher.
* **Role/visitor audit**
  * `modules/housekeeping/role_audit.py`
    * `resolve_audit_destination()` resolves `ADMIN_AUDIT_DEST_ID` with log-channel fallbacks.
    * `_member_roles()` extracts member role IDs.
    * `_classify_roles()` classifies Raid/Wandering/clan-role combinations.
    * `_extra_roles()` calculates Visitor extra roles.
    * `_audit_guild()` scans guild members and builds all audit buckets.
    * `_apply_role_changes()` performs role mutations only when non-dry-run and actor is not scheduled/background/startup-like.
    * `_render_report()` renders the Discord embed sections.
    * `run_role_and_visitor_audit()` resolves config, aggregates guild results, and sends the embed.
    * `preview_role_audit_mutations()` recomputes a dry-run mutation snapshot for the admin command.
* **Report rendering / command access**
  * `modules/housekeeping/role_audit.py::_render_report()` renders detected/action sections.
  * `cogs/recruitment_reporting.py::RecruitmentReporting.report_group()` exposes `!report recruiters [all]`.
  * `cogs/recruitment_reporting.py::RecruitmentReporting.roleaudit()` exposes `!roleaudit preview` and `!roleaudit apply CONFIRM [override]`.
* **Config loading used by the audit**
  * `shared/config.py` functions imported by `modules/housekeeping/role_audit.py`: `get_admin_audit_dest_id`, `get_log_channel_id`, `get_logging_channel_id`, `get_allowed_guild_ids`, `get_clan_role_ids`, `get_promo_channel_id`, `get_raid_role_id`, `get_visitor_role_id`, `get_wandering_souls_role_id`, and `get_welcome_channel_id`.
  * `modules/common/feature_flags.py` is used by the daily recruiter report feature gate through `feature_enabled()`.
  * Ticket data comes from `modules/common/tickets.py::fetch_ticket_threads()` and `TicketThread`.

## Automatic Role Removal Investigation

Searches performed included `remove_roles`, `member.remove_roles`, role edits via `roles=`, role cleanup, visitor cleanup, clan role cleanup, Wandering Souls handling, pruning, scheduled cleanup, startup/backfill/repair, watcher tasks, button flows, ticket close flows, and admin commands.

### Role-removal paths found

#### 1. Role & Visitor Audit apply path

* **File/function:** `modules/housekeeping/role_audit.py::_apply_role_changes()`, called by `_audit_guild()` and `run_role_and_visitor_audit()`.
* **Trigger/entrypoint:** `!roleaudit apply CONFIRM [override]` via `cogs/recruitment_reporting.py::RecruitmentReporting.roleaudit()`.
* **Which roles can be removed:** The configured Raid role.
* **Conditions for removal:**
  * `stray`: member has Raid, no configured clan role, and no Wandering Souls; apply removes Raid and adds Wandering Souls.
  * `drop_raid`: member has Raid and Wandering Souls, but no configured clan role; apply removes Raid.
* **Automatic or staff/admin action:** Staff/admin action only for actual mutation. Scheduled/background/startup-like actors and dry runs are explicitly report-only and skip mutations.

#### 2. Daily scheduled Role & Visitor Audit dry-run

* **File/function:** `modules/recruitment/reporting/daily_recruiter_update.py::run_full_recruiter_reports()` calls `modules/housekeeping/role_audit.py::run_role_and_visitor_audit()`.
* **Trigger/entrypoint:** Daily scheduler at `REPORT_DAILY_POST_TIME`, plus manual `!report recruiters all`.
* **Which roles can be removed:** None in current scheduled behavior.
* **Conditions for removal:** Not applicable. The call uses `dry_run=True`, and `_apply_role_changes()` also refuses to mutate for scheduled/background/startup-like actors.
* **Automatic or staff/admin action:** Automatic report only; no role removal.

#### 3. Clan role remove command

* **File/function:** `cogs/clanrole_management.py::ClanRoleManagementCog.apply_clan_removal_cleanup()`.
* **Trigger/entrypoint:** Staff command `!clanrole remove <member query>`, including a selection view when multiple members or clan roles match.
* **Which roles can be removed:** One selected configured clan role; also the configured Raid role if the member has no remaining configured clan roles after the selected clan role is removed.
* **Conditions for removal:** Caller must pass the command authorization check. If exactly one clan role is selected, the command removes it. If no other configured clan roles remain and Raid is present, Raid is removed. The command may add Wandering Souls afterward.
* **Automatic or staff/admin action:** Staff/admin action only.

#### 4. Fusion opt-out button

* **File/function:** `modules/community/fusion/opt_in_view.py` button handler.
* **Trigger/entrypoint:** User presses the Fusion opt-out button.
* **Which roles can be removed:** The configured Fusion opt-in role for that fusion panel.
* **Conditions for removal:** The user is a guild member, the role exists, the button action is opt-out, and the member currently has the role.
* **Automatic or staff/admin action:** User self-service action, not automatic.

#### 5. Fusion ended-role cleanup

* **File/function:** `modules/community/fusion/role_cleanup.py::process_ended_fusion_role_cleanup()`.
* **Trigger/entrypoint:** Fusion scheduler/cleanup processing for fusions returned by `fusion_sheets.get_ended_fusions()`.
* **Which roles can be removed:** A fusion row's configured opt-in role (`opt_in_role_id`).
* **Conditions for removal:** Fusion is ended, an opt-in role ID exists, the cleanup dedupe key has not already been marked sent, the guild and role resolve, and members are present in `role.members`.
* **Automatic or staff/admin action:** Automatic scheduled cleanup.

#### 6. Shard reminder opt-out button

* **File/function:** `modules/community/shard_tracker/cog.py` reminder role update handler.
* **Trigger/entrypoint:** User presses shard reminder opt-out UI.
* **Which roles can be removed:** The selected clan shard reminder opt-in role.
* **Conditions for removal:** Selected config has an opt-in role ID, the guild/member/role resolve, action is opt-out, and the member has the role.
* **Automatic or staff/admin action:** User self-service action, not automatic.

#### 7. Reaction-role revoke

* **File/function:** `modules/community/reaction_roles.py` reaction-role event handler.
* **Trigger/entrypoint:** A configured reaction-role event where `grant` is false, typically reaction removal.
* **Which roles can be removed:** The configured reaction-role row's role ID.
* **Conditions for removal:** The reaction payload matches configured row location, the role exists, and the member currently has the role.
* **Automatic or staff/admin action:** Event-driven user self-service action; automatic in response to the reaction event.

#### 8. Reset reminder opt-out button

* **File/function:** `modules/community/reset_reminders/views.py::ResetReminderView` button callback.
* **Trigger/entrypoint:** User presses reset reminder opt-out UI.
* **Which roles can be removed:** The configured reset reminder role for the view.
* **Conditions for removal:** Guild/member/role resolve and action is opt-out.
* **Automatic or staff/admin action:** User self-service action, not automatic.

### Searched but no role-removal path found

* **Visitor cleanup:** No code path was found that automatically removes Visitor roles as part of housekeeping or ticket close handling.
* **Clan role cleanup outside staff command:** No automatic clan-role removal path was found outside `!clanrole remove`.
* **Wandering Souls removal:** No current path was found that removes Wandering Souls. The role audit may add Wandering Souls in manual apply mode; the clan-role command may add Wandering Souls after a clan removal.
* **Ticket close flows:** Welcome/promo close handlers update sheets, reservations/open spots, thread names, prompts, and logs. No member role removal was found in those ticket close paths.
* **Startup repair/backfill:** Welcome/promo close backfill finalizes sheet/ticket state; no member role removal was found there.
* **Housekeeping cleanup watcher:** Deletes messages from configured cleanup threads; does not remove roles.
* **Pruning:** No Discord member-pruning role-removal implementation was found.

## Role Removal Logging

#### 1. Role & Visitor Audit apply path

* **Discord ops/admin logs:** The manual apply sends the Role & Visitor Audit embed to the audit destination. When `dry_run=False`, `_render_report()` includes action sections for roles removed/added and failures/skips.
* **App logs:** `_apply_role_changes()` logs successful mutations with before/after role names, removed roles, added roles, actor, member ID/name, and reason. Permission/API failures are warning logs with member/error context.
* **Silent?** Not silent for the admin command path because it replies to the command and posts the audit/action report. Scheduled runs are dry-run only and log that mutations were skipped.
* **Deduped/suppressed?** No dedupe for the audit report was found.
* **Failures logged clearly?** Yes, failures are logged and included in the report's failed/skipped action section when apply mode is used.

#### 2. Clan role remove command

* **Discord ops/admin logs:** The command replies in-channel with the removal/cleanup result, but no separate ops-log post was found for the mutation.
* **App logs:** Successful clan role removal, Raid removal, and Wandering Souls add are logged with member/actor context. Failures log exceptions.
* **Silent?** Not silent to the command invoker, but not necessarily visible in a centralized admin ops log unless the command channel itself is monitored.
* **Deduped/suppressed?** No dedupe found.
* **Failures logged clearly?** Yes to app logs and command response.

#### 3. Fusion opt-out button

* **Discord ops/admin logs:** Failures call `fusion_logs.send_ops_alert(...)`. Successful self-service opt-outs do not appear to send an ops/admin log.
* **App logs:** Failures are logged with exception/context. Success is acknowledged ephemerally to the user; no success app log was found in the inspected snippet.
* **Silent?** Successful removals are silent to admins, but user-initiated and limited to Fusion opt-in roles.
* **Deduped/suppressed?** Failure ops alerts use dedupe keys.
* **Failures logged clearly?** Yes; failure alerts and app logs exist.

#### 4. Fusion ended-role cleanup

* **Discord ops/admin logs:** Failures for loading ended fusions, status transitions, dedupe state, and iteration failures call `fusion_logs.send_ops_alert(...)`. Per-member removal failures are only app logged. Successful automatic removals do not appear to send an admin-visible summary.
* **App logs:** Missing guild/role and per-member failures are logged. Successful removals are not individually logged in the inspected code.
* **Silent?** Yes. This is an automatic role-removal path, and successful role removals can occur without an admin-visible success log or summary.
* **Deduped/suppressed?** Cleanup uses a sheet dedupe key so each ended fusion cleanup is only processed once; failure ops alerts use dedupe keys.
* **Failures logged clearly?** Some failures are visible via ops alerts; per-member removal failures are app-log-only.

#### 5. Shard reminder opt-out button

* **Discord ops/admin logs:** None found for success or failure.
* **App logs:** Generic exception logs for unexpected failures; `discord.Forbidden` only replies to the user and does not log in the inspected snippet.
* **Silent?** Successful removals are silent to admins, but user-initiated and limited to reminder opt-in roles.
* **Deduped/suppressed?** No dedupe found.
* **Failures logged clearly?** Unexpected failures are app logged; forbidden failures are user-visible but not app/ops logged in the inspected snippet.

#### 6. Reaction-role revoke

* **Discord ops/admin logs:** None found for success or failure in the inspected code path.
* **App logs:** Failures log exceptions with action/key/role/user context. Success logs an info entry after applying rows.
* **Silent?** Admin-visible Discord logs were not found; revokes are event-driven and user-triggered by reaction removal.
* **Deduped/suppressed?** No dedupe found.
* **Failures logged clearly?** Failures are app logged.

#### 7. Reset reminder opt-out button

* **Discord ops/admin logs:** None found.
* **App logs:** Missing role warns; forbidden and unexpected failures log exceptions with context.
* **Silent?** Successful removals are silent to admins, but user-initiated and limited to reset reminder roles.
* **Deduped/suppressed?** No dedupe found.
* **Failures logged clearly?** Yes in app logs for missing role, forbidden, and unexpected failures.

## Findings Summary

1. **Are roleless/no-role members currently checked?** No. They are scanned and counted, but no roleless/no-meaningful-role condition is evaluated or reported.
2. **Why are they missing from the report?** The audit has no section for roleless/no-meaningful-role members. Members without Raid, Wandering Souls, Visitor, or configured clan roles fall through `_classify_roles()` as `ok`, and Visitor checks are skipped unless the member has Visitor.
3. **Can the bot remove roles automatically?** Yes, but not through the daily Role & Visitor Audit. The confirmed automatic role-removal path is Fusion ended-role cleanup, which removes ended fusion opt-in roles. Reaction-role revocation is also event-driven and can remove roles in response to reaction events. The daily Role & Visitor Audit runs as dry-run/report-only.
4. **Are automatic role removals visible in admin logs?** Not consistently. Fusion ended-role cleanup can successfully remove roles without an admin-visible success summary. Some failures are sent to Fusion ops alerts, but successful removals and per-member failures are not consistently admin-visible. User self-service opt-outs generally acknowledge the user and may log failures, but do not post admin-visible success logs.
5. **What behavior is confirmed safe?** The scheduled daily Role & Visitor Audit is confirmed report-only: it passes `dry_run=True`, and `_apply_role_changes()` refuses to mutate roles for scheduled/background/startup-like actors. Ticket close/backfill and housekeeping message cleanup were not found to remove member roles.
6. **What behavior needs follow-up?** Add a roleless/no-meaningful-role reporting bucket if admins want those members visible. Add admin-visible logging/summaries for any automatic role removals, especially Fusion ended-role cleanup, before treating automatic removals as operationally transparent.

## Recommended Follow-Up

Do not implement code yet. Safest follow-up design:

* **Add roleless/no-role members to the existing Role & Visitor Audit report.** This belongs in the current audit because it already scans all guild members and posts to the admin audit destination.
* **Suggested report section name:** `Roleless / no meaningful roles` or `Members with only @everyone`. If the check becomes broader than literal `@everyone` only, use `Members with no configured allowed roles`.
* **Make the check sheet/config-driven.** Avoid hard-coding what counts as "meaningful" beyond `@everyone`. Admin expectations may include staff roles, bot roles, event opt-in roles, muted roles, or other non-clan roles.
* **Possible config needed later:**
  * A config key or sheet row for enabled/disabled state, for example `roleless_member_audit_enabled`.
  * A configured allowed-role list, for example `ROLELESS_AUDIT_ALLOWED_ROLE_IDS` or a sheet-backed `AllowedRoles`/`RoleAuditAllowedRoles` tab.
  * Optional ignore role IDs, ignore user IDs, ignore bot accounts, minimum account age/join age, and max rows to display.
  * A display label/notes column if using a sheet tab, so admins can document why a role is allowed/ignored.
* **Suggested criteria if implemented later:**
  * Literal roleless bucket: member role IDs minus the guild `@everyone` role is empty.
  * Broader no-meaningful-role bucket: member has no configured clan role, no Visitor, no Raid, no Wandering Souls, and no role in a configured allowed-role list. Decide explicitly whether bot accounts are excluded.
* **Logging for automatic role removals:**
  * Every automatic role-removal job should post an admin-visible summary to the relevant ops/audit destination with job name, role IDs/names, member count, success count, skipped count, failure count, and dedupe key/status.
  * Per-member details should be available in the summary or an attached/truncated detail section when counts are small.
  * Failures should always be both app-logged and admin-visible, including permission/hierarchy failures.
  * Success logs should not be entirely silent for automatic removals. If noise is a concern, aggregate summaries are safer than per-member spam.

## Implementation Follow-Up (2026-06-20)

This follow-up implemented the audit changes recommended by the investigation without adding sheet tabs, sheet columns, or new config keys.

* Members whose only role is the guild `@everyone` role are now collected in the existing Role & Visitor Audit and rendered in a `Members with only @everyone` section. Bot accounts are excluded from this section so automated service users do not create noise in the human housekeeping list.
* The scheduled Role & Visitor Audit remains report-only/dry-run for role mutations.
* Fusion ended-role cleanup now persists compact unreported cleanup summaries in the existing Fusion reminder/dedupe storage, while retaining runtime state only as a fallback. The scheduled Role & Visitor Audit reads those summaries and renders them in a `Fusion role cleanup` section using the existing audit destination.
* Cleanup summaries are marked reported only after the scheduled Role & Visitor Audit successfully sends; manual audit/report runs do not clear scheduled cleanup visibility.
* No separate Fusion cleanup report or standalone daily Fusion cleanup message was added.
* No report result counts are capped. The audit renderer can split oversized output across multiple embeds/messages while preserving all section lines.
