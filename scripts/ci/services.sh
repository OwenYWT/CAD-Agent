#!/usr/bin/env bash
# All resources belong to this invocation; existing services are never reused.
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
runtime=${SANDBOX_COMMAND:-docker}
python=${CAD_CI_PYTHON:-python}
scope=${CAD_CI_SCOPE:?unique cad-ci scope required}
[[ $scope == cad-ci-* && $scope != *[^a-z0-9-]* ]] || { printf 'Invalid CI resource scope\n' >&2; exit 2; }
envfile=${CAD_CI_ENV_FILE:?private CI environment file required}
action=${1:?up or down}
receipt="$envfile.owner"
if [[ $action == down ]]; then
  [[ -f $receipt ]] || exit 0
  owner=$(cat "$receipt")
  for name in temporal minio postgres; do
    owned=$("$runtime" inspect "$scope-$name" --format '{{index .Config.Labels "cad.ci.owner"}}' 2>/dev/null || true)
    if [[ $owned == "$owner" ]]; then "$runtime" rm --force "$scope-$name"; fi
  done
  owned=$("$runtime" network inspect "$scope" --format '{{index .Labels "cad.ci.owner"}}' 2>/dev/null || true)
  if [[ $owned == "$owner" ]]; then "$runtime" network rm "$scope"; fi
  rm -f "$receipt" "$envfile"
  exit
fi
[[ $action == up && ! -e $envfile && ! -e $receipt ]] || { printf 'Refusing to overwrite CI environment\n' >&2; exit 2; }
if "$runtime" network inspect "$scope" >/dev/null 2>&1; then
  printf 'Refusing to reuse existing network: %s\n' "$scope" >&2; exit 2
fi
for name in postgres minio temporal; do
  if "$runtime" inspect "$scope-$name" >/dev/null 2>&1; then
    printf 'Refusing to reuse existing container: %s\n' "$scope-$name" >&2; exit 2
  fi
done
secret=$("$python" -c 'import secrets;print(secrets.token_hex(24))')
if [[ ${GITHUB_ACTIONS:-} == true ]]; then printf '::add-mask::%s\n' "$secret"; fi
owner=$("$python" -c 'import secrets;print(secrets.token_hex(24))')
bucket="$scope-artifacts"
[[ ${#bucket} -le 63 ]] || { printf 'CI object-store scope is too long\n' >&2; exit 2; }
umask 077
printf '%s' "$owner" > "$receipt"
pg_port=${CAD_CI_PG_PORT:-55432}
s3_port=${CAD_CI_S3_PORT:-59000}
temporal_port=${CAD_CI_TEMPORAL_PORT:-57233}
"$runtime" network create --label "cad.ci.owner=$owner" "$scope"
"$runtime" run -d --name "$scope-postgres" --label "cad.ci.owner=$owner" --network "$scope" \
  -e POSTGRES_DB=cad_ci -e POSTGRES_USER=cad_ci -e POSTGRES_PASSWORD="$secret" \
  -p "127.0.0.1:$pg_port:5432" postgres:16.14-alpine3.24
for attempt in {1..60}; do
  if "$runtime" exec "$scope-postgres" pg_isready -U cad_ci -d cad_ci; then break; fi
  test "$attempt" -lt 60; sleep 1
done
"$runtime" run -d --name "$scope-minio" --label "cad.ci.owner=$owner" --network "$scope" \
  -e MINIO_ROOT_USER=cad_ci -e MINIO_ROOT_PASSWORD="$secret" -p "127.0.0.1:$s3_port:9000" \
  "${CAD_CI_OBJECT_STORE_IMAGE:?verified MinIO image required}" server /data
for attempt in {1..60}; do
  if "$python" -c 'import sys,urllib.request;urllib.request.urlopen(sys.argv[1],timeout=5).close()' \
    "http://127.0.0.1:$s3_port/minio/health/live" 2>/dev/null; then break; fi
  test "$attempt" -lt 60; sleep 1
done
"$runtime" run --rm --network "$scope" --entrypoint /bin/sh -e MINIO_ROOT_PASSWORD="$secret" \
  -e CI_MINIO_HOST="$scope-minio" "$CAD_CI_OBJECT_STORE_IMAGE" -ec \
  "mc alias set local http://\$CI_MINIO_HOST:9000 cad_ci \"\$MINIO_ROOT_PASSWORD\" >/dev/null; mc mb --ignore-existing local/$bucket; mc version enable local/$bucket"
"$runtime" run -d --name "$scope-temporal" --label "cad.ci.owner=$owner" --network "$scope" \
  -e DB=postgres12 -e DB_PORT=5432 -e POSTGRES_SEEDS="$scope-postgres" \
  -e POSTGRES_USER=cad_ci -e POSTGRES_PWD="$secret" -e DEFAULT_NAMESPACE=default \
  -p "127.0.0.1:$temporal_port:7233" temporalio/auto-setup:1.29.7
for attempt in {1..120}; do
  if "$runtime" exec "$scope-temporal" temporal operator cluster health --address "$scope-temporal:7233"; then break; fi
  test "$attempt" -lt 120; sleep 1
done
umask 077
cat > "$envfile" <<ENV
export APP_ENVIRONMENT=test
export DURABLE_CONTROL_PLANE_ENABLED=true
export DATABASE_URL=postgresql+asyncpg://cad_ci:$secret@127.0.0.1:$pg_port/cad_ci
export CAD_AGENT_TEST_DATABASE_URL=postgresql+asyncpg://cad_ci:$secret@127.0.0.1:$pg_port/cad_ci
export CAD_AGENT_TEST_OBJECT_STORE=1
export CAD_AGENT_TEST_TEMPORAL=1
export TEMPORAL_TARGET=127.0.0.1:$temporal_port
export TEMPORAL_TASK_QUEUE=$scope-v1
export TEMPORAL_AGENT_V2_TASK_QUEUE=$scope-v2
export OBJECT_STORE_ENDPOINT_URL=http://127.0.0.1:$s3_port
export OBJECT_STORE_ACCESS_KEY=cad_ci
export OBJECT_STORE_SECRET_KEY=$secret
export OBJECT_STORE_BUCKET=$bucket
export SANDBOX_RUNTIME=docker
export SANDBOX_COMMAND=$runtime
export SANDBOX_IMAGE=${SANDBOX_IMAGE:?verified sandbox image required}
export CAD_MODEL_JOB_TEST_DATABASE_URL=postgresql+asyncpg://cad_ci:$secret@127.0.0.1:$pg_port/cad_model_contracts
export CAD_MODEL_JOB_TEST_TEMPORAL=127.0.0.1:$temporal_port
export CAD_MODEL_JOB_TEST_WAIT_SECONDS=310
export CAD_MONITOR_TEST_DATABASE_URL=postgresql+asyncpg://cad_ci:$secret@127.0.0.1:$pg_port/cad_monitor_contracts
export CAD_MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://cad_ci:$secret@127.0.0.1:$pg_port/cad_upgrade_contracts
export CAD_RELEASE_TEST_DATABASE_URL=postgresql+asyncpg://cad_ci:$secret@127.0.0.1:$pg_port/cad_release_contracts
ENV
source "$envfile"
export PYTHONPATH="$root:$root/backend"
for database in cad_model_contracts cad_monitor_contracts cad_upgrade_contracts cad_release_contracts; do
  "$runtime" exec "$scope-postgres" createdb -U cad_ci "$database"
done
cd "$root/backend"
for database in cad_ci cad_model_contracts cad_monitor_contracts cad_release_contracts; do
  DATABASE_URL="postgresql+asyncpg://cad_ci:$secret@127.0.0.1:$pg_port/$database" "$python" -m alembic upgrade head
done
if [[ -n ${GITHUB_ENV:-} ]]; then sed 's/^export //' "$envfile" >> "$GITHUB_ENV"; fi
