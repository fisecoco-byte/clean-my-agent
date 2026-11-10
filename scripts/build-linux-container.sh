#!/usr/bin/env bash
# Build Linux artifacts locally, including on a Mac with Docker.
set -euo pipefail
target="${1:-linux-arm64}"
case "$target" in linux-arm64) docker_platform=linux/arm64;; linux-x64) docker_platform=linux/amd64;; *) exit 2;; esac
repo=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$repo/dist"
docker run --rm -i --platform "$docker_platform" \
  --mount "type=bind,source=$repo,target=/src,readonly" \
  --mount "type=bind,source=$repo/dist,target=/out" \
  --env "CMA_BUILD_TARGET=$target" "${CMA_UBUNTU_IMAGE:-ubuntu:22.04}" bash -s <<'CONTAINER'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv libpython3.10 binutils git lsof curl >/dev/null
python3 -m venv /opt/cma-build
/opt/cma-build/bin/python -m pip install --quiet --upgrade pip setuptools wheel
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
mkdir /app
cp -R /src/clean_my_agent /src/scripts /src/skills /src/docs /src/tests /app/
cp /src/pyproject.toml /src/README.md /src/clean-my-agent /src/install.sh /src/install.ps1 /app/
cd /app
/opt/cma-build/bin/python -m pip install --quiet --use-pep517 '.[ui,mcp]' pyinstaller==6.22.3
/opt/cma-build/bin/python -c 'import textual, mcp; from clean_my_agent import __version__; print("Building", __version__)'
/opt/cma-build/bin/python -m unittest discover -s tests -q
/opt/cma-build/bin/python scripts/build-release.py --target "$CMA_BUILD_TARGET"
/opt/cma-build/bin/python scripts/smoke-release.py "build/release/$CMA_BUILD_TARGET/binary/clean-my-agent"
cp dist/clean-my-agent-* /out/
CONTAINER
