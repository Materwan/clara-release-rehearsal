#!/usr/bin/env bash
# Build the web site (web/ → src/clara/web/dist) in a pinned Node image: no Node on the host, no root in the container.
#   scripts/build-web.sh          install (npm ci), type-check (svelte-check), test (the folder reader), build
#   scripts/build-web.sh --quick  skip npm ci when node_modules is there
set -euo pipefail

NODE_IMAGE="${NODE_IMAGE:-docker.io/library/node@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402}"  # node 22.23.3
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENGINE="${CONTAINER_ENGINE:-$(command -v podman || command -v docker)}"

install="npm ci --no-audit --no-fund"
if [[ "${1:-}" == "--quick" && -d "$ROOT/web/node_modules" ]]; then
  install="true"
fi

# Files written into the tree must belong to you: rootless Podman keeps your id inside, Docker is told it.
if [[ "$(basename "$ENGINE")" == podman ]]; then
  as_you=(--userns=keep-id)
else
  as_you=(--user "$(id -u):$(id -g)")
fi

"$ENGINE" run --rm "${as_you[@]}" \
  -e HOME=/tmp -e npm_config_cache=/tmp/npm-cache -e npm_config_update_notifier=false \
  -v "$ROOT:/repo" -w /repo/web "$NODE_IMAGE" \
  sh -c "$install && npm run check && npm test && npm run build"

echo "Built: $ROOT/src/clara/web/dist"
