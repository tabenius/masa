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
./masa --help >/dev/null
"$PYTHON" -c "import importlib.metadata; import masa_cli; importlib.metadata.version('masa-google-takeout-compressor')"
