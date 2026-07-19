#!/usr/bin/env bash
set -euo pipefail

# Fail CI if deprecated get_port import path is used.
# Exclusions: AUDIT, VCS, envs, node_modules, build caches.

shopt -s globstar nullglob

echo "🔍 Guardrail: scanning for forbidden runtime get_port imports..."

if ! command -v rg >/dev/null 2>&1; then
  echo "❌ ripgrep (rg) is required for the forbidden-import guardrail." >&2
  exit 2
fi

set +e
matches=$(rg -n --hidden --no-ignore \
  -g '!AUDIT/**' \
  -g '!tests/**' \
  -g '!**/.git/**' \
  -g '!**/node_modules/**' \
  -g '!**/.venv/**' \
  -g '!**/venv/**' \
  -g '!**/dist/**' \
  -g '!**/build/**' \
  "(from\\s+(shared\\.config|config\\.runtime)\\s+import[^\\n]*\\bget_port\\b|(shared\\.config|config\\.runtime)\\.get_port)")
rg_status=$?
set -e

if [[ ${rg_status} -gt 1 ]]; then
  echo "❌ ripgrep failed while scanning for forbidden imports." >&2
  exit "${rg_status}"
fi

if [[ -n "${matches}" ]]; then
  echo "❌ Forbidden import path detected:"
  echo "${matches}"
  echo
  echo "Use 'from shared.ports import get_port' instead."
  exit 1
fi

echo "✅ No forbidden imports found."
