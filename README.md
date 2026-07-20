# C1C Bot - The Woadkeeper

**Version:** v0.9.8.2

The Woadkeeper is the C1C cluster's unified Discord bot for recruitment,
onboarding, community tools, and day-to-day operations. It connects Discord
workflows with sheet-backed configuration and records so members, recruiters,
and administrators can use one consistent set of tools.

This README is the repository landing page. For setup, operation, and command
details, use the documentation links below rather than treating this page as an
admin manual.

## Current Documentation

- **Live admin/operator wiki:** [C1C Bot wiki](https://github.com/cailleanC1C/C1C-Bot/wiki)
- **Version-controlled wiki source:** [`docs/wiki/`](docs/wiki/)
- **Implementation/reference docs:** [`docs/README.md`](docs/README.md)

The wiki is maintained as code: its Markdown sources live in `docs/wiki/` and
are published to GitHub Wiki pages. See the
[wiki command reference](https://github.com/cailleanC1C/C1C-Bot/wiki/Command-Reference)
for available commands, permissions, and invocation details; the full command
catalogue is intentionally not duplicated here.

## What the Woadkeeper Covers

- **CoreOps and runtime:** health, configuration, checks, cache refreshes,
  reloads, help, operational digests, and runtime safeguards.
- **Onboarding and promo tickets:** guided welcome and promotion flows,
  question capture, routing, summaries, and handoff to placement.
- **Recruitment and clan operations:** member and recruiter panels, clan search
  and profiles, clan advertising, recruitment reporting, placement, and seat
  reservations.
- **Community features:** shard tracking, Fusion and Titan events, reset
  reminders, progress guides, C1C leagues, and reaction roles.
- **Housekeeping:** cleanup, keepalive, achievements, the achievement collector,
  Wandering Souls diagnostics, Realmwalker audits, Mirralith overviews, C1C ads,
  role audits, and the guides help index.
- **Discord roles and permissions:** role-aware access, interactive permission
  management, server navigation, and role-holder overviews.

For an administrator-oriented map of these areas and their dependencies, see
the [feature index](https://github.com/cailleanC1C/C1C-Bot/wiki/Feature-Index).

## Repository Guide

- [`docs/wiki/`](docs/wiki/) contains the source of the operator-facing wiki.
- [`docs/README.md`](docs/README.md) indexes architecture, operations,
  troubleshooting, module deep dives, governance, and other implementation
  references.
- [`docs/contracts/CollaborationContract.md`](docs/contracts/CollaborationContract.md)
  defines contribution and documentation standards.
- [`docs/adr/`](docs/adr/) records significant architectural decisions.

Runtime configuration and operational procedures belong in the wiki and
reference documentation. Secrets and credential values must never be committed
to this repository.

Doc last updated: 2026-07-20 (v0.9.8.2)
