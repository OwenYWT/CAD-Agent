#!/usr/bin/env bash
# Only disposable GitHub Linux VMs; never a developer or self-hosted machine.
# RUNNER_ENVIRONMENT: https://docs.github.com/en/actions/reference/workflows-and-actions/variables
set -euo pipefail
if [[ ${GITHUB_ACTIONS:-} != true || ${RUNNER_ENVIRONMENT:-} != github-hosted || ${RUNNER_OS:-} != Linux ]]; then
  printf 'Refusing resource cleanup outside a GitHub-hosted Linux runner\n' >&2
  exit 2
fi
df -h /
# CAD jobs do not use .NET, Android, Haskell or CodeQL. Keep Python, Node,
# Docker, the checkout, caches, and all application data untouched.
sudo rm -rf -- /usr/share/dotnet /usr/local/lib/android /usr/local/.ghcup \
  /opt/ghc /opt/hostedtoolcache/CodeQL
df -h /
available_kib=$(df --output=avail -k / | tail -n 1)
if (( available_kib < 24 * 1024 * 1024 )); then
  printf 'CAD CI requires 24 GiB free disk for verified images and build layers; available: %s KiB\n' "$available_kib" >&2
  exit 1
fi
