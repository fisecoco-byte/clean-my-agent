#!/usr/bin/env bash
# Build and validate the native Rust program in Linux, including on a Mac.
set -euo pipefail
target="${1:-linux-arm64}"
case "$target" in linux-arm64) docker_platform=linux/arm64;; linux-x64) docker_platform=linux/amd64;; *) exit 2;; esac
repo=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$repo/dist"
docker run --rm -i --platform "$docker_platform" \
  --mount "type=bind,source=$repo,target=/src,readonly" \
  --mount "type=bind,source=$repo/dist,target=/out" \
  --env "CMA_BUILD_TARGET=$target" "${CMA_RUST_IMAGE:-public.ecr.aws/docker/library/rust:slim-bookworm}" bash -s <<'CONTAINER'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends python3 git lsof procps ca-certificates >/dev/null
mkdir /app
cp -R /src/rust /src/assets /src/scripts /src/skills /src/docs /app/
cp /src/Cargo.toml /src/Cargo.lock /src/build.rs /src/README.md /src/README_CN.md /src/install.sh /src/install.ps1 /app/
cd /app
cargo test --locked
python3 scripts/build-release.py --target "$CMA_BUILD_TARGET"
cp dist/clean-my-agent-* /out/
CONTAINER
