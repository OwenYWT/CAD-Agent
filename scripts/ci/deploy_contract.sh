#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
exec "${CAD_CI_PYTHON:-python}" "$root/scripts/ci/deploy_contract.py"
