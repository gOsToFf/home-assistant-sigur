#!/usr/bin/env bash
# Create the Linux development virtualenv used to run the Home Assistant tests.
#
# Home Assistant core cannot be imported on Windows (it imports `fcntl`), so the
# HA-dependent part of the test suite runs under WSL or any Linux host. The
# protocol-only tests under tests/ run anywhere.
set -euo pipefail

VENV="${SIGUR_VENV:-$HOME/.venvs/sigur}"
PYTHON_VERSION="${SIGUR_PYTHON:-3.13}"

if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"

uv venv --python "$PYTHON_VERSION" "$VENV"

# Home Assistant still pins lru-dict 1.3.0, which has no wheel for Python 3.13
# and needs a C toolchain to build. 1.4.1 is API compatible and ships wheels.
OVERRIDES="$(mktemp)"
printf 'lru-dict==1.4.1\n' > "$OVERRIDES"

uv pip install --python "$VENV/bin/python" --overrides "$OVERRIDES" \
  pytest-homeassistant-custom-component

rm -f "$OVERRIDES"

"$VENV/bin/python" - <<'PY'
import sys
from homeassistant.const import __version__

print(f"Home Assistant {__version__} on Python {sys.version.split()[0]}")
PY

echo "Done. Run the suite with: $VENV/bin/python -m pytest"
