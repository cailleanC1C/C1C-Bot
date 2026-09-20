# Cluster history and evaluation

## Status

This document records the cluster evaluation model agreed in September 2026 and the
production history-capture contract that feeds the `ClusterEvaluation` sheet.

The evaluation itself remains sheet-owned. The bot owns weekly capture, result-week
attribution, event scheduling, status semantics, idempotency, and the human-readable
capture summary.

## Weekly capture

The league publishing job runs for a posting week, but cluster history belongs to the
completed result week immediately before it. For example, a W38 publishing job captures
W37 results. ISO-week arithmetic is used so year boundaries are safe.

Hydra and Chimera are weekly. CvC and Siege alternate from the authoritative anchor:

- 2026-W31 CvC
- 2026-W32 Siege
- 2026-W33 CvC
- 2026-W34 Siege

CvC also alternates PR / Non-PR from W31 = PR, so W33 = Non-PR, W35 = PR, and so on.

`ClusterClans` is the clan registry. Each active clan has a per-event expectation:
`Mandatory`, `Optional`, or `N/A`.

- Mandatory: missing submissions are meaningful and retained for later evaluation.
- Optional: results are captured when present; missing is neutral.
- N/A: no history candidate is generated for that clan/event.
- N/A only affects bracket-movement evidence. Existing numeric performance data may
  still be used for within-bracket ranking.

The bot accepts `cluster_clans_tab` and temporarily falls back to the legacy
`cluster_clan_map_tab` config key.

## History schema and statuses

`ClusterEventHistory` has 14 columns:

`record_key, week_key, event_type, clan_tag, clan_name, score, score_unit, result,
event_class, evaluation_status, captured_at_utc, source_range, source_row, source_trigger`

The only evaluation statuses are:

- `valid`: complete available result captured.
- `missing`: clan row was found but no submitted result was present.
- `error`: expected source/config data could not be matched or parsed reliably.

For CvC and Siege, win/loss is the complete result, so those rows are `valid`.
`result_only` is retired.

An event that did not occur that week generates no evaluable rows and never enters a
denominator.

For weekly score events, blank and zero remain `missing` for compatibility with the
existing capture rule; malformed or negative values are `error`. Revisit zero semantics
only if the league owners define a different domain rule.

History is append-only and retry-safe. Identical record keys/payloads are deduplicated;
a conflicting payload raises a history conflict rather than overwriting history.

## CvC evaluation semantics

All CvC wins count. PR losses count. Non-PR losses do not enter the relevant-result
denominator. Non-PR losses are still valid history.

## Current bracket evaluation model

Bracket movement and within-bracket ranking are deliberately separate.

### Suggested bracket

Hydra is the primary current-strength PvE signal. Recent median represents current form;
all-time PB represents demonstrated capability.

Hydra PB protects a clan against premature demotion. The current conservative policy is
to retain that protection until there is roughly 13 weeks of evidence. A result at or
above 90% of PB is currently considered "near PB". PB alone does not promote a clan.

Chimera is asymmetric because Trials can reward deliberately lower damage. Historical
Chimera PB remains demonstrated capability. High recent Chimera scores are positive
evidence, but low recent scores do not independently prove decline. There is no
13-week/90%-PB decay rule for Chimera.

Siege and CvC are competitive supporting signals. They can resolve appropriate ambiguity
but do not replace the PvE evidence.

Active clans always receive a real suggested bracket and an action of Move up, Move down,
or Stay. Insufficient history does not manufacture movement evidence.

### Within-bracket order

Ranking is only among clans whose **suggested bracket is the same**. It is not a global
power ranking.

Higher number means stronger. A four-clan bracket is ranked 1..4, with 4 strongest.

Hydra and Chimera numeric strength are the primary comparison. Existing numbers remain
usable for ranking even when that event is N/A for bracket movement. Siege/CvC are used
only when the PvE evidence does not clearly distinguish the clans.

Current regression/sanity checks:

- Elite: TornsValhalla 1, IslandSacred 2, Martyrs 3, Elders 4.
- Beginner: Novus 1, Descendants 2.
- The September 2026 review also confirmed the current Early Game, Mid Game, Late Game,
  and Endgame ordering as sensible.

Persistent Mandatory non-submission is intended to lower within-bracket position later,
but the exact penalty has intentionally **not** been invented yet. Optional missing, N/A,
off-week events, and errors are neutral for performance.

## Capture summary

Discord should report the result week and meaningful outcomes, for example:

- Hydra: valid / mandatory missing / optional missing / errors
- Chimera: valid / mandatory missing / optional missing / errors
- CvC or Siege: the scheduled event; the other is shown as not scheduled
- total data errors are surfaced separately

Append/existing candidate bookkeeping remains useful in logs, not as the primary human
status message.

## Next review: 13 weeks of data

The current history covers approximately W31-W37 2026. Around seven more weekly captures
should give roughly 13 weeks of observations.

**Target review: early November 2026 (around 2026-W44).**

At that point, review the model before allowing historical Hydra protection to expire.
Specifically decide what "sustained decline" means. Do not silently choose a rule now.
Compare real clan histories and decide whether it should mean consecutive weeks, a
13-week median, a percentage of weeks below 90% PB, or another robust measure.

Also review whether 90% of PB is the right "near PB" threshold, whether Mandatory-missing
needs a ranking penalty and how large it should be, and whether the recent Siege/CvC
windows are giving stable enough evidence.

Do not add a Chimera decline rule merely because 13 weeks have elapsed; the Trials/damage
trade-off remains the reason low Chimera damage is not reliable negative evidence.

## Operational next steps

1. Let the corrected weekly capture run and inspect its Discord summary after each league
   publication for unexpected errors or missing mappings.
2. Keep `ClusterClans` expectations current when clan requirements change.
3. Keep `ClusterCaptureConfig` source ranges aligned with Stormforged layout changes.
4. At the 13-week review, evaluate the Hydra protection rule and Mandatory-missing policy
   using actual history before changing the formulas.
5. Preserve the Elite and Beginner sanity checks as regression fixtures whenever ranking
   logic changes.

---

Doc last updated: 2026-09-20 (v0.9.8.3)
