#!/usr/bin/env bash
# Pin the existing closure by default. Dependency upgrades require an explicit diff.
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
cd "$root"
python=${CAD_CI_PYTHON:-python}
"$python" -m uv pip compile backend/requirements.txt --constraint backend/requirements.lock \
  --python-version 3.11 --python-platform x86_64-unknown-linux-gnu --no-annotate -o backend/requirements.lock
"$python" -m uv pip compile backend/requirements-dev.txt --constraint backend/requirements.lock \
  --constraint backend/requirements-ci.lock --python-version 3.11 --python-platform x86_64-unknown-linux-gnu \
  --no-annotate -o backend/requirements-ci.lock
