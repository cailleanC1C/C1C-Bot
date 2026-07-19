# Repository Hygiene Audit

## Cleaned

The tracked and untracked files were inspected for generated Python bytecode,
tool caches, build and distribution output, temporary files, and logs. No
generated artifacts matching those categories were present or tracked, so no
generated files required removal.

## Ignored

The repository now ignores Python bytecode and interpreter caches; pytest,
Ruff, mypy, and coverage output; packaging and build output; and local
temporary and log output.

## Intentionally Not Touched

No application code, runtime or scheduler logic, Google Sheets integration,
imports, commands, modules, files, or functions were changed or renamed.

The existing `member_panel.py` and `member_panel_legacy.py` files were not
changed or deleted. Member panel cleanup is deferred to a separate, safe PR.

---

Doc last updated: 2026-07-19 (v0.9.8.2)
