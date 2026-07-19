#!/usr/bin/env bash
set -euo pipefail

# Fail CI if deprecated get_port import path is used.
# Exclusions: AUDIT, VCS, envs, node_modules, build caches.

shopt -s globstar nullglob

echo "🔍 Guardrail: scanning for forbidden imports (shared.config → get_port)..."

matches=$(rg -n --hidden --no-ignore \
  -g '!AUDIT/**' \
  -g '!**/.git/**' \
  -g '!**/node_modules/**' \
  -g '!**/.venv/**' \
  -g '!**/venv/**' \
  -g '!**/dist/**' \
  -g '!**/build/**' \
  "(from\\s+shared\\.config\\s+import[^\\n]*\\bget_port\\b|shared\\.config\\.get_port)" || true)

if [[ -n "${matches}" ]]; then
  echo "❌ Forbidden import path detected:"
  echo "${matches}"
  echo
  echo "Use 'from shared.ports import get_port' instead."
  exit 1
fi

echo "✅ No forbidden imports found."
