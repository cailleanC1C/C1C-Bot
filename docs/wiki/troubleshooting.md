# Troubleshooting

## Fast triage

1. Record environment, guild, feature, command/job, timestamp, and human-readable destination—never secrets.
2. Check `!health`, `!digest`, `!next`, and recent startup/feature logs.
3. Determine whether the fault is Discord permissions, Sheet schema/config, cache staleness, scheduler state, rate limiting, or code/deploy.
4. Apply the smallest reversible fix and retry once with a preview/dry-run where available.

| Symptom | Checks | Safe response |
|---|---|---|
| Command missing/denied | Feature toggle, access metadata, role policy, guild allow-list, prefix/mention route | Correct source config/role; do not weaken global checks |
| Stale data | Cache age, last refresh result, Config pointer, headers | `!refresh <bucket>`; validate output |
| Sheet/tab/header error | Workbook ENV ID, sharing, Config tab pointer, required header/alias | Have Sheet owner correct live Sheet; run `!checksheet` |
| Ticket panel does not open/resume | watcher readiness, parent scope, thread membership, persistent views, schema hash/session | Fix access/config; reload schema or deliberately resume |
| Scheduled post missing | feature toggle, scheduler registration, timezone/date, source rows, destination permission | Correct source and run one preview/manual job |
| Duplicate post/job | reconnect/startup logs, job tags, persisted message/session ID, interaction retry | Stop manual retries; reconcile idempotently |
| Missing Access / Permissions | parent/category visibility, thread membership, overwrites, role hierarchy | Grant only required permission and retry once |
| Sheets 429/quota | request burst, backoff, scheduler/manual overlap, cache usage | Stop refresh loop and follow rate-limit triage |
| Wiki publish fails | local validator, Wiki initialized, token/repo permission, target branch | Fix validation/access; rerun workflow |

Do not “fix” production by editing undocumented live cells, exposing a secret, hard-coding an ID, disabling guardrails, or repeatedly restarting. Escalate with redacted logs and exact reproduction. See [[Operations Runbook]] for detailed procedures.

Doc last updated: 2026-07-20 (v0.9.8.2)
