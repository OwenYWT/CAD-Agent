#!/usr/bin/env bash
set -euo pipefail
runtime=${SANDBOX_COMMAND:-docker}
image=${SANDBOX_IMAGE:?SANDBOX_IMAGE required}
root=$(cd "$(dirname "$0")/../.." && pwd)
report=${CAD_NATIVE_CONTRACT_REPORT:?CAD_NATIVE_CONTRACT_REPORT required}
mkdir -p "$report"
for script in freecad_constraint_diagnostics freecad_reference_fixture; do
  "$runtime" run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
    --tmpfs /tmp:rw,size=2g --tmpfs /sandbox/input:rw,mode=777 --tmpfs /sandbox/output:rw,mode=777 \
    -v "$root/backend/tests/e2e:/tests:ro" --entrypoint /opt/freecad/bin/FreeCADCmd "$image" \
    -c "exec(compile(open('/tests/$script.py').read(), '/tests/$script.py', 'exec'))" > "$report/$script.log" 2>&1
  # FreeCADCmd can return zero for a Python exception; require the actual report.
  if [[ "$script" == freecad_constraint_diagnostics ]]; then
    rg 'CAD_CONSTRAINT_DIAGNOSTICS=' "$report/$script.log"
  else
    rg 'CAD_REFERENCE_GEOMETRY=' "$report/$script.log"
  fi
done
"$runtime" run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /tmp:rw,size=2g --tmpfs /sandbox/input:rw,mode=777 --tmpfs /sandbox/output:rw,mode=777 \
  -v "$root/backend/tests/e2e:/tests:ro" --entrypoint python "$image" \
  /tests/freecad_engineering_result_channel.py > "$report/engineering.log" 2>&1
