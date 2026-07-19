#!/usr/bin/env bash
set -euo pipefail

# Fail CI if deprecated get_port import path is used.
# Exclusions: AUDIT, VCS, envs, node_modules, build caches.

shopt -s globstar nullglob

echo "🔍 Guardrail: scanning for forbidden get_port imports..."

set +e
if command -v rg >/dev/null 2>&1; then
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
  search_status=$?
else
  echo "ℹ️ ripgrep unavailable; using grep fallback."
  matches=$(grep -RInE --binary-files=without-match \
    --exclude-dir=AUDIT \
    --exclude-dir=tests \
    --exclude-dir=.git \
    --exclude-dir=node_modules \
    --exclude-dir=.venv \
    --exclude-dir=venv \
    --exclude-dir=dist \
    --exclude-dir=build \
    -e 'from[[:space:]]+(shared\.config|config\.runtime)[[:space:]]+import.*get_port' \
    -e '(shared\.config|config\.runtime)\.get_port' \
    .)
  search_status=$?
fi
set -e

if (( search_status > 1 )); then
  echo "❌ Search failed while scanning forbidden imports (status ${search_status})."
  exit "${search_status}"
fi

if [[ -n "${matches}" ]]; then
  echo "❌ Forbidden import path detected:"
  echo "${matches}"
  echo
  echo "Use 'from shared.ports import get_port' instead."
  exit 1
fi

echo "✅ No forbidden imports found."
