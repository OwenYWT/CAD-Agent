#!/usr/bin/env bash
set -euo pipefail
runtime=${SANDBOX_COMMAND:-docker}
image=${SANDBOX_IMAGE:?SANDBOX_IMAGE required}
root=$(cd "$(dirname "$0")/../.." && pwd)
report=${CAD_NATIVE_CONTRACT_REPORT:?CAD_NATIVE_CONTRACT_REPORT required}
mkdir -p "$report"
"${CAD_CI_PYTHON:-python}" "$root/backend/tests/e2e/scoped_repair_fixture.py" "$report/scoped-repair.json"
"${CAD_CI_PYTHON:-python}" "$root/backend/tests/e2e/parameter_dependency_fixture.py" "$report/parameter-dependency.json"
"${CAD_CI_PYTHON:-python}" "$root/backend/tests/e2e/source_obligation_fixture.py" "$report/source-obligations.json"
common=("$runtime" run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp:rw,mode=1777,size=2g --tmpfs /sandbox/input:rw,mode=777 --tmpfs /sandbox/output:rw,mode=777 -v "$root/backend/tests/e2e:/tests:ro" -v "$report/scoped-repair.json:/sandbox/input/scoped-repair.json:ro" -v "$report/parameter-dependency.json:/sandbox/input/parameter-dependency.json:ro")
for script in freecad_cut_diagnostics_contract freecad_scoped_repair_contract freecad_profile_replan_contract freecad_constraint_diagnostics freecad_constraint_failure_snapshot freecad_constraint_profile_contract freecad_constraint_parameter_contract freecad_parameter_dependencies_contract freecad_product_acceptance_contract freecad_step_import_contract freecad_reference_fixture chamfer_scope_geometry freecad_coordinate_contract freecad_profile_contract freecad_curve_profile_contract freecad_hole_cut_contract freecad_loft_contract freecad_sweep_contract freecad_revolve_contract freecad_pattern_contract freecad_tangency_contract freecad_repair_replay_contract freecad_api_contract freecad_tool_replay_contract freecad_checkpoint_replay_contract; do
  retention=()
  if [[ "$script" == freecad_parameter_dependencies_contract ]]; then
    mkdir -p "$report/parameter-dependency-artifacts"
    chmod 777 "$report/parameter-dependency-artifacts"
    retention=(-e CAD_DEPENDENCY_EVIDENCE_DIR=/evidence -v "$report/parameter-dependency-artifacts:/evidence")
  fi
  invocation=("${common[@]}" "${retention[@]}" --entrypoint /opt/freecad/bin/FreeCADCmd "$image" -c "exec(compile(open('/tests/$script.py').read(), '/tests/$script.py', 'exec'))")
  "${invocation[@]}" > "$report/$script.log" 2>&1
  # FreeCADCmd can return zero for a Python exception; require the actual report.
  if [[ "$script" == freecad_constraint_diagnostics ]]; then
    grep -F 'CAD_CONSTRAINT_DIAGNOSTICS=' "$report/$script.log"
  elif [[ "$script" == freecad_reference_fixture ]]; then
    grep -F 'CAD_REFERENCE_GEOMETRY=' "$report/$script.log"
  elif [[ "$script" == chamfer_scope_geometry ]]; then
    grep -F 'CAD_CHAMFER_SCOPE=' "$report/$script.log"
  else
    name=${script#freecad_}
    marker=$(printf '%s' "$name" | tr '[:lower:]' '[:upper:]')
    grep -F "CAD_${marker}=" "$report/$script.log"
  fi
  if grep -Fq 'Traceback (most recent call last)' "$report/$script.log"; then exit 1; fi
  "${CAD_CI_PYTHON:-python}" "$root/scripts/ci/step_metadata.py" "$report" "$script" \
    "${invocation[@]}"
done
invocation=("${common[@]}" -e PYTHONPATH=/opt/cad-agent --entrypoint python "$image" /tests/acceptance_comparison_v2_contract.py)
"${invocation[@]}" > "$report/acceptance-comparison-v2.log" 2>&1
grep -F 'CAD_ACCEPTANCE_COMPARISON_V2=' "$report/acceptance-comparison-v2.log"
"${CAD_CI_PYTHON:-python}" "$root/scripts/ci/step_metadata.py" "$report" acceptance-comparison-v2 "${invocation[@]}"
invocation=("${common[@]}" -e PYTHONPATH=/opt/cad-agent --entrypoint python "$image" /tests/component_measurement_contract.py)
"${invocation[@]}" > "$report/component-measurement.log" 2>&1
grep -F 'CAD_COMPONENT_MEASUREMENT_CONTRACT=' "$report/component-measurement.log"
"${CAD_CI_PYTHON:-python}" "$root/scripts/ci/step_metadata.py" "$report" component-measurement "${invocation[@]}"
mkdir -p "$report/source-obligation-artifacts"
chmod 777 "$report/source-obligation-artifacts"
invocation=("${common[@]}" -v "$report/source-obligation-artifacts:/evidence" -e CAD_SOURCE_EVIDENCE_DIR=/evidence -v "$report/source-obligations.json:/sandbox/input/source-obligations.json:ro" -e PYTHONPATH=/opt/cad-agent --entrypoint python "$image" /tests/source_obligation_contract.py)
"${invocation[@]}" > "$report/source-obligations.log" 2>&1
grep -F 'CAD_SOURCE_OBLIGATION_CONTRACT=' "$report/source-obligations.log"
"${CAD_CI_PYTHON:-python}" "$root/scripts/ci/step_metadata.py" "$report" source-obligations "${invocation[@]}"
invocation=("${CAD_CI_PYTHON:-python}" "$root/backend/tests/e2e/source_obligation_fixture.py" --verify "$report/source-obligations.log")
"${invocation[@]}" > "$report/source-obligations-receiver.log" 2>&1
grep -F 'CAD_SOURCE_RECEIVER_CONTRACT=' "$report/source-obligations-receiver.log"
"${CAD_CI_PYTHON:-python}" "$root/scripts/ci/step_metadata.py" "$report" source-obligations-receiver "${invocation[@]}"
invocation=("${common[@]}" -e PYTHONPATH=/opt/cad-agent --entrypoint python "$image" /tests/measurement_binding_contract.py)
"${invocation[@]}" > "$report/measurement-binding.log" 2>&1
grep -F 'CAD_MEASUREMENT_BINDING_CONTRACT=' "$report/measurement-binding.log"
"${CAD_CI_PYTHON:-python}" "$root/scripts/ci/step_metadata.py" "$report" measurement-binding "${invocation[@]}"
invocation=("${common[@]}" -e PYTHONPATH=/opt/cad-agent --entrypoint python "$image" /tests/planar_gap_contract.py)
"${invocation[@]}" > "$report/planar-gap.log" 2>&1
grep -F 'CAD_PLANAR_GAP_CONTRACT=' "$report/planar-gap.log"
"${CAD_CI_PYTHON:-python}" "$root/scripts/ci/step_metadata.py" "$report" planar-gap "${invocation[@]}"
invocation=("${common[@]}" -e PYTHONPATH=/opt/cad-agent --entrypoint python "$image" /tests/feature_verification_contract.py)
"${invocation[@]}" > "$report/feature-measurements.log" 2>&1
grep -F 'FEATURE_MEASUREMENT_CONTRACT_PASSED' "$report/feature-measurements.log"
"${CAD_CI_PYTHON:-python}" "$root/scripts/ci/step_metadata.py" "$report" feature-measurements "${invocation[@]}"
invocation=("${common[@]}" -e PYTHONPATH=/opt/cad-agent --entrypoint python "$image" /tests/curved_surface_contract.py)
"${invocation[@]}" > "$report/curved-surfaces.log" 2>&1
grep -F 'CURVED_SURFACE_CONTRACT=' "$report/curved-surfaces.log"
"${CAD_CI_PYTHON:-python}" "$root/scripts/ci/step_metadata.py" "$report" curved-surfaces "${invocation[@]}"
invocation=("${common[@]}" -e PYTHONPATH=/opt/cad-agent --entrypoint python "$image" /tests/freecad_engineering_result_channel.py)
"${invocation[@]}" > "$report/engineering.log" 2>&1
grep -F 'CAD_ENGINEERING_RESULT_CHANNEL=' "$report/engineering.log"
"${CAD_CI_PYTHON:-python}" "$root/scripts/ci/step_metadata.py" "$report" engineering "${invocation[@]}"
invocation=("${common[@]}" -e PYTHONPATH=/opt/cad-agent --entrypoint python "$image" /tests/cadquery_svg_contract.py)
"${invocation[@]}" > "$report/cadquery-svg.log" 2>&1
grep -F 'CADQUERY_SVG_CONTRACT=' "$report/cadquery-svg.log"
"${CAD_CI_PYTHON:-python}" "$root/scripts/ci/step_metadata.py" "$report" cadquery-svg "${invocation[@]}"
