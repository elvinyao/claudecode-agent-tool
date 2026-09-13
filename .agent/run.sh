#!/usr/bin/env bash
set -euo pipefail

if [[ $# -eq 0 ]]; then
  echo "usage: bash .agent/run.sh <command> [args...]" >&2
  exit 64
fi

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_IMAGE="ghcr.io/astral-sh/uv:python3.12-bookworm-slim"
# Git is absent from the slim uv image; keep Git operations in Docker too.
if [[ "$1" == "git" ]]; then
  DEFAULT_IMAGE="python:3.12-bookworm"
fi
AGENT_RUN_IMAGE="${AGENT_RUN_IMAGE:-${DEFAULT_IMAGE}}"
HOST_USER_ID="$(id -u)"
HOST_GROUP_ID="$(id -g)"

docker run --rm -i --init \
  --user "${HOST_USER_ID}:${HOST_GROUP_ID}" \
  -v "${PROJECT_ROOT}:/workspace" \
  -w /workspace \
  -e UV_CACHE_DIR=/tmp/uv-cache \
  "${AGENT_RUN_IMAGE}" \
  "$@"
