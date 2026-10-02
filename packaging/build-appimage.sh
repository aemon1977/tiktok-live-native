#!/bin/bash
# Builds dist/TikTok-LIVE-Native-x86_64.AppImage in a clean Ubuntu 24.04 container (needs Docker).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
docker run --rm -v "$ROOT:/src" -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" ubuntu:24.04 bash /src/packaging/inside.sh
ls -lh "$ROOT/dist/"
