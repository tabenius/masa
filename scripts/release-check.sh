#!/usr/bin/env bash
set -euo pipefail

PYTHON_CMD="$(command -v python3 || command -v python)"
case "$PYTHON_CMD" in
  /*) PYTHON="$PYTHON_CMD" ;;
  *) PYTHON="$PWD/$PYTHON_CMD" ;;
esac

"$PYTHON" -m compileall src/python tests
"$PYTHON" -m pytest
"$PYTHON" -m ruff check src/python tests
"$PYTHON" -m ruff format --check src/python tests
"$PYTHON" -m build --no-isolation
"$PYTHON" -m pip install --no-deps --force-reinstall dist/*.whl >/dev/null
masa --help >/dev/null
