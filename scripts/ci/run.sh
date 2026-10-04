#!/usr/bin/env bash
# Local and Actions runs select the same suites. Environment setup is explicit.
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
command=${1:?Usage: run.sh quick [backend|frontend|architecture] | core | lifecycle | browser | deploy}
selection=${2:-all}
python=${CAD_CI_PYTHON:-python}
export PYTHONPATH="$root:$root/backend${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1 APP_ENVIRONMENT=test
# This entry only runs deterministic suites, including on a developer machine
# that already has provider credentials. Paid evaluation has its own entry.
export MOONSHOT_API_KEY= DASHSCOPE_API_KEY= AZURE_OPENAI_API_KEY= ANTHROPIC_API_KEY=
case "$command:$selection" in
  core:*) default_job=mcad-core ;; lifecycle:*) default_job=model-lifecycle ;;
  browser:*) default_job=browser-e2e ;; deploy:*) default_job=deploy-smoke ;;
  quick:architecture) default_job=architecture-contract ;; quick:backend|quick:frontend) default_job=$selection ;;
  *) default_job=$command ;;
esac
job=${CAD_CI_JOB:-$default_job}
report=${CAD_CI_REPORT_ROOT:-$(mktemp -d "$root/reports-local-$job.XXXXXX")}
[[ ! -f $report/job.json ]] || { printf 'Refusing to reuse evidence directory: %s\n' "$report" >&2; exit 2; }
mkdir -p "$report"
report=$(cd "$report" && pwd)
"$python" "$root/scripts/ci/job_metadata.py" "$job" "$report"
step=preflight
finish() {
  result=$?
  if [[ $result -ne 0 ]]; then
    printf 'Failed step: %s\nReproduce: bash scripts/ci/run.sh %q %q\nLogs: %s\n' "$step" "$command" "$selection" "$report" | tee "$report/failure.txt"
    if [[ -f $report/$step.log ]]; then
      printf 'Failure log excerpt:\n' | tee -a "$report/failure.txt"
      tail -n 30 "$report/$step.log" | tee -a "$report/failure.txt"
    fi
    if [[ -n ${GITHUB_STEP_SUMMARY:-} ]]; then cat "$report/failure.txt" >> "$GITHUB_STEP_SUMMARY"; fi
  fi
  exit "$result"
}
trap finish EXIT
stage() {
  step=$1
  shift
  "$@" 2>&1 | tee "$report/$step.log"
  "$python" "$root/scripts/ci/step_metadata.py" "$report" "$step" "$@"
}
suite() {
  local name=$1 file=$2
  shift 2
  stage "$name" "$python" -m pytest -p scripts.ci.pytest_contract "$@" -q --junitxml="$report/$file"
  "$python" "$root/scripts/ci/require_test_report.py" "$report/$file" --suite "$name"
}
case "$command" in
  quick)
    case $selection in backend|frontend|architecture|all) ;; *) exit 2;; esac
    if [[ $selection == backend || $selection == all ]]; then
      cd "$root/backend"
      export DURABLE_CONTROL_PLANE_ENABLED=false MOONSHOT_API_KEY= DASHSCOPE_API_KEY= AZURE_OPENAI_API_KEY=
      stage backend-lint "$python" -m ruff check --select E9,F63,F7,F82 \
        --per-file-ignores 'app/contracts/constraint_patch.py:F821' \
        app/contracts/constraint_patch.py app/freecad/failure_snapshot.py app/freecad/constraint_relationships.py \
        app/freecad/constraint_patch.py app/workflows/constraint_evidence.py \
        app/workflows/handlers/native_generation.py app/workflows/handlers/cad_execution.py
      stage backend-types "$python" -m mypy --strict app/domain/identity.py app/domain/projects.py
      stage backend "$python" -m pytest -p scripts.ci.pytest_contract -m 'not docker and not llm and not fusion_e2e' -q --junitxml="$report/backend.xml"
      "$python" "$root/scripts/ci/require_test_report.py" "$report/backend.xml" --suite backend
      cd "$root"
      stage fusion-schema "$python" scripts/fusion360/generate_schema.py --check
      stage fusion-syntax "$python" -m compileall -q backend/app/fusion360 fusion_addin/CADAgentFusionConnector scripts/fusion360
      stage shell-syntax bash -c 'for script in scripts/fusion360/*.sh scripts/ci/*.sh; do bash -n "$script" || exit; done'
      cd "$root/backend"
      suite fusion fusion.xml tests/fusion360 -m 'not fusion_e2e'
      cd "$root"
      stage ci-contracts "$python" -m pytest -p scripts.ci.pytest_contract scripts/ci/tests -q --junitxml="$report/ci-contracts.xml"
      "$python" "$root/scripts/ci/require_test_report.py" "$report/ci-contracts.xml" --suite ci-contracts
    fi
    if [[ $selection == frontend || $selection == all ]]; then
      cd "$root/frontend"
      stage frontend-lint npm run lint
      stage frontend-tests node --test --experimental-strip-types --test-reporter=spec --test-reporter=junit --test-reporter-destination=stdout --test-reporter-destination="$report/frontend.xml" tests/*.test.ts
      "$python" "$root/scripts/ci/require_test_report.py" "$report/frontend.xml" --suite frontend
      stage frontend-types npx tsc --noEmit -p tsconfig.app.json
      stage frontend-build npx vite build
    fi
    if [[ $selection == architecture || $selection == all ]]; then
      cd "$root"
      stage boundaries "$python" scripts/architecture/check_boundaries.py
      stage feature-map "$python" .agents/skills/cad-agent-review/scripts/validate_feature_map.py
      stage ci-policy "$python" scripts/ci/check_policy.py
    fi
    ;;
  core)
    export MOONSHOT_API_KEY= DASHSCOPE_API_KEY= AZURE_OPENAI_API_KEY=
    export CAD_AGENT_TEST_EVIDENCE_DIR="$report/cad-artifacts" CAD_CONSTRAINT_REPORT="$report/constraint-chain"
    export CAD_CI_RETAIN_FAILURE_PLANS=1
    cd "$root/backend"
    suite durable-core durable-core.xml \
      tests/integration/test_temporal_mcadd_workflow.py tests/integration/test_websocket_replay.py \
      tests/integration/test_change_set_api.py tests/integration/test_postgres_project_files.py \
      tests/integration/test_postgres_history.py tests/integration/test_api_compatibility_matrix.py \
      -k 'not real_freecad_generation_validation and not real_visual_provider and not real_repair_provider and not real_planner_retriever and not real_generate_runs_llm and not live_freecad_tools_contract and not live_freecad_checkpoint_contract'
    native_nodes=()
    while IFS= read -r node; do native_nodes+=("$node"); done < <("$python" -c "from scripts.ci.require_test_report import required_cases;print('\n'.join(required_cases('native-runtime')))")
    # Explicit resource budget for the locked CAD kernel/import graph. The
    # native OOM regression still exceeds this budget and must fail closed.
    RUN_REAL_PODMAN=1 RUN_REAL_FREECAD=1 SANDBOX_MEMORY_LIMIT=1g suite native-runtime native-runtime.xml "${native_nodes[@]}"
    stage dynamic-replay "$python" tests/e2e/replay_histories.py --output "$report/workflow-replay.json"
    stage released-replay "$python" tests/e2e/replay_released_histories.py --output "$report/released-workflow-replay.json"
    cd "$root"
    export CAD_NATIVE_CONTRACT_REPORT="$report/native"
    stage native bash scripts/ci/native_contracts.sh
    ;;
  lifecycle)
    export CAD_CI_RETAIN_FAILURE_PLANS=1
    cd "$root/backend"
    suite transactions transactions.xml tests/postgres --ignore=tests/postgres/test_model_jobs.py --ignore=tests/postgres/test_monitoring.py --ignore=tests/postgres/test_upgrade_contract.py
    suite data-integration data-integration.xml tests/integration \
      --ignore=tests/integration/test_temporal_mcadd_workflow.py --ignore=tests/integration/test_websocket_replay.py \
      --ignore=tests/integration/test_change_set_api.py --ignore=tests/integration/test_postgres_project_files.py \
      --ignore=tests/integration/test_postgres_history.py --ignore=tests/integration/test_api_compatibility_matrix.py
    suite model-lifecycle model-lifecycle-db.xml tests/postgres/test_model_jobs.py
    stage lifecycle-replay "$python" tests/e2e/replay_histories.py --workflow-types ModelJobWorkflow --output "$report/workflow-replay.json"
    suite usage-monitoring usage-monitoring.xml tests/postgres/test_monitoring.py
    suite schema-upgrade schema-upgrade.xml tests/postgres/test_upgrade_contract.py
    cd "$root"
    stage cross-release "$python" backend/tests/e2e/release_source_compatibility.py --old "${CAD_CI_RELEASE_BASELINE:?select immutable released source}" --current "$root" --output "$report/release-source-drill"
    ;;
  browser)
    export CAD_BROWSER_REPORT_ROOT="$report"
    cd "$root"
    stage browser bash scripts/ci/browser_contract.sh
    ;;
  deploy)
    cd "$root"
    stage deploy bash scripts/ci/deploy_contract.sh
    ;;
  *) printf 'Unknown CI command: %s\n' "$command" >&2; exit 2 ;;
esac
