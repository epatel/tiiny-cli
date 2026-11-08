#!/bin/sh
# PreToolUse(Bash): block pip installs that don't target the project venv.
# Fails open if jq is missing.
command -v jq >/dev/null 2>&1 || exit 0
cmd=$(jq -r '.tool_input.command // empty')
case "$cmd" in
  *"pip install"*|*"pip3 install"*|*"-m pip install"*) ;;
  *) exit 0 ;;
esac
case "$cmd" in
  *".venv/bin/pip"*|*".venv/bin/python"*) exit 0 ;;
esac
echo "Blocked: install dependencies into the project venv (.venv/bin/pip install ...), never the system/user Python. Add runtime deps to pyproject.toml." >&2
exit 2
