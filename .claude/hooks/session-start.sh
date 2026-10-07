#!/bin/bash
# Готовит окружение в облачных сессиях Claude Code: venv + зависимости проекта + dev-инструменты.
# Идемпотентен; локально ничего не делает (там вы управляете окружением сами, см. `make install`).
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-.}"

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi

.venv/bin/pip install --quiet --disable-pip-version-check -e ".[dev]"

# Чтобы pytest, ruff, mypy и mailbot были доступны в сессии без префикса .venv/bin/
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "export PATH=\"$PWD/.venv/bin:\$PATH\"" >> "$CLAUDE_ENV_FILE"
fi
