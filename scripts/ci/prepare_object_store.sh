#!/usr/bin/env bash
set -euo pipefail
destination=${1:?select a fresh build context}
mkdir -p "$destination"
curl --fail --silent --show-error --location --retry 3 \
  https://github.com/minio/minio/releases/download/RELEASE.2025-09-07T16-13-09Z/minio.linux-amd64.RELEASE.2025-09-07T16-13-09Z -o "$destination/minio"
curl --fail --silent --show-error --location --retry 3 \
  https://github.com/minio/mc/releases/download/RELEASE.2025-08-13T08-35-41Z/mc.linux-amd64.RELEASE.2025-08-13T08-35-41Z -o "$destination/mc"
printf '7c5bd8512c6e966455b1d198209358b2d191c77a83ab377c4073281065fb855f  %s/minio\n' "$destination" | sha256sum --check --strict
printf '01f866e9c5f9b87c2b09116fa5d7c06695b106242d829a8bb32990c00312e891  %s/mc\n' "$destination" | sha256sum --check --strict
chmod 755 "$destination/minio" "$destination/mc"
cat > "$destination/Dockerfile" <<'DOCKERFILE'
FROM alpine:3.24
RUN apk add --no-cache curl
COPY minio mc /usr/local/bin/
ENTRYPOINT ["/usr/local/bin/minio"]
DOCKERFILE
