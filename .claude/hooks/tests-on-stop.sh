#!/bin/sh
# Stop: if src/ or tests/ have uncommitted changes, the unit tests must pass
# before the agent finishes. Tests are mocked (no device needed) and take <1s.
cd "$CLAUDE_PROJECT_DIR" || exit 0
[ -x .venv/bin/pytest ] || exit 0
# Loop guard: never block a second time in a row (works with or without jq).
input=$(cat)
if command -v jq >/dev/null 2>&1; then
  [ "$(printf '%s' "$input" | jq -r '.stop_hook_active // false')" = "true" ] && exit 0
else
  printf '%s' "$input" | grep -q '"stop_hook_active" *: *true' && exit 0
fi
[ -z "$(git status --porcelain -- src tests 2>/dev/null)" ] && exit 0
if ! res=$(.venv/bin/pytest -q -x 2>&1); then
  printf 'Unit tests fail — fix before finishing:\n%s\n' "$(printf '%s' "$res" | tail -30)" >&2
  exit 2
fi
exit 0
