#!/usr/bin/env bash
# End-user installer: no Python, pip or administrator rights required.
set -euo pipefail
REPO="${CMA_REPO:-fisecoco-byte/clean-my-agent}"
BIN_DIR="${CMA_BIN_DIR:-$HOME/.local/bin}"
VERSION="${CMA_VERSION:-latest}"
SOURCE=""
CHANGE_PATH=1
ACTION=install
fail() { printf '安装未完成：%s\n' "$*" >&2; exit 1; }
while [[ $# -gt 0 ]]; do
    case "$1" in
        --version|--from|--bin-dir|--repo)
            [[ $# -ge 2 ]] || fail "$1 缺少参数"
            case "$1" in
                --version) VERSION="$2";; --from) SOURCE="$2";;
                --bin-dir) BIN_DIR="$2";; --repo) REPO="$2";;
            esac; shift 2;;
        --no-path) CHANGE_PATH=0; shift;;
        --uninstall) ACTION=uninstall; shift;;
        --help) printf '%s\n' 'install.sh [--version v0.1.0] [--from BINARY] [--bin-dir DIR] [--no-path] [--uninstall]'; exit 0;;
        *) fail "未知参数：$1";;
    esac
done
[[ "$BIN_DIR" == /* && "$BIN_DIR" != / ]] || fail '安装目录必须是绝对路径'
[[ "$BIN_DIR" != *$'\n'* && "$BIN_DIR" != *$'\r'* ]] || fail '安装目录包含换行'
[[ "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || fail '仓库格式应为 owner/repo'
TARGET="$BIN_DIR/clean-my-agent"
MARKER="$BIN_DIR/.clean-my-agent-install"
START='# >>> clean-my-agent PATH >>>'
END='# <<< clean-my-agent PATH <<<'
PROFILES=("$HOME/.profile" "$HOME/.bashrc" "${ZDOTDIR:-$HOME}/.zprofile" "${ZDOTDIR:-$HOME}/.zshrc")
# Creating .bash_profile would hide an existing .profile on login.
for profile in "$HOME/.bash_profile" "$HOME/.bash_login"; do
    if [[ -f "$profile" ]]; then PROFILES+=("$profile"); fi
done
quote() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"; }
if [[ "$ACTION" == uninstall ]]; then
    [[ -f "$MARKER" && "$(cat "$MARKER")" == "$TARGET" && ! -L "$TARGET" ]] || fail '没有找到本安装器管理的程序'
    rm -f -- "$TARGET" "$MARKER"
    for profile in "${PROFILES[@]}"; do
        [[ -f "$profile" ]] || continue
        scratch=$(mktemp)
        awk -v begin="$START" -v end="$END" '$0==begin {skip=1;next} $0==end {skip=0;next} !skip {print}' "$profile" > "$scratch"
        cat "$scratch" > "$profile"
        rm -f -- "$scratch"
    done
    fish_file="${XDG_CONFIG_HOME:-$HOME/.config}/fish/conf.d/clean-my-agent.fish"
    if [[ -f "$fish_file" ]] && grep -Fq -- "$START" "$fish_file"; then rm -f -- "$fish_file"; fi
    printf '%s\n' '已卸载程序；策略、报告和暂存数据保留。'
    exit 0
fi
case "$(uname -s)" in Darwin) PLATFORM=macos;; Linux) PLATFORM=linux;; *) fail 'Windows 请使用 install.ps1';; esac
case "$(uname -m)" in arm64|aarch64) ARCH=arm64;; x86_64|amd64) ARCH=x64;; *) fail '仅支持 x64 / arm64';; esac
if [[ "$PLATFORM" == linux ]] && command -v ldd >/dev/null 2>&1; then
    if ldd --version 2>&1 | grep -qi musl; then fail '当前 Linux 包需要 glibc 2.35+（Ubuntu 22.04+），不适用于 Alpine'; fi
fi
TMP_INSTALL=$(mktemp -d "${TMPDIR:-/tmp}/clean-my-agent.XXXXXX")
PENDING=""
trap 'rm -rf -- "$TMP_INSTALL"; if [[ -n "$PENDING" ]]; then rm -f -- "$PENDING"; fi' EXIT
fetch() { curl --fail --location --silent --show-error --proto '=https' --tlsv1.2 --retry 3 --connect-timeout 15 "$1" -o "$2"; }
sha() { if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1"; else shasum -a 256 "$1"; fi; }
# Downloaded packages contain this marker; a source checkout does not.
BUNDLE_DIR=$(cd "$(dirname "$0")" 2>/dev/null && pwd || true)
if [[ -z "$SOURCE" && "${0##*/}" == install.sh && -f "$0" && -f "$BUNDLE_DIR/BUNDLE_TARGET" ]]; then
    [[ "$(cat "$BUNDLE_DIR/BUNDLE_TARGET")" == "$PLATFORM-$ARCH" ]] || fail '安装包与当前系统架构不匹配'
    SOURCE="$BUNDLE_DIR/clean-my-agent"
    VERSION=$(cat "$BUNDLE_DIR/VERSION")
fi
if [[ -n "$SOURCE" ]]; then
    [[ -f "$SOURCE" && ! -L "$SOURCE" ]] || fail '本地程序不存在或是链接'
    cp -- "$SOURCE" "$TMP_INSTALL/clean-my-agent"
else
    command -v curl >/dev/null || fail '需要 curl 下载发布包'
    if [[ "$VERSION" == latest ]]; then
        latest=$(curl --fail --location --silent --show-error --proto '=https' --tlsv1.2 --retry 3 --connect-timeout 15 -o /dev/null -w '%{url_effective}' "https://github.com/$REPO/releases/latest")
        VERSION=${latest##*/}
    fi
    [[ "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[A-Za-z0-9.-]+)?$ ]] || fail '未找到有效 Release；请先发布 vX.Y.Z 标签'
    asset="clean-my-agent-$VERSION-$PLATFORM-$ARCH.tar.gz"
    base="https://github.com/$REPO/releases/download/$VERSION"
    printf '下载 %s…\n' "$asset"
    fetch "$base/$asset" "$TMP_INSTALL/package.tar.gz"
    fetch "$base/SHA256SUMS" "$TMP_INSTALL/SHA256SUMS"
    expected=$(awk -v file="$asset" '$2==file {print $1}' "$TMP_INSTALL/SHA256SUMS")
    [[ "$expected" =~ ^[0-9a-fA-F]{64}$ ]] || fail '发布包缺少有效校验值'
    actual=$(sha "$TMP_INSTALL/package.tar.gz"); actual=${actual%% *}
    [[ "$actual" == "$expected" ]] || fail 'SHA256 校验失败，保留原安装'
    # Extract exactly one member to stdout; archive paths never become filesystem paths.
    tar -xOzf "$TMP_INSTALL/package.tar.gz" clean-my-agent/clean-my-agent > "$TMP_INSTALL/clean-my-agent"
fi
chmod 755 "$TMP_INSTALL/clean-my-agent"
probe=$("$TMP_INSTALL/clean-my-agent" --version) || fail '程序无法在此系统运行，保留原安装'
[[ "$probe" == 'clean-my-agent '* ]] || fail '不是 Clean My Agent 程序'
if [[ "$VERSION" != latest ]]; then [[ "$probe" == "clean-my-agent ${VERSION#v}" ]] || fail '版本与发布包不符'; fi
mkdir -p -- "$BIN_DIR"
[[ ! -L "$TARGET" ]] || fail '目标是现有链接，请先选择其他安装目录'
if [[ -e "$TARGET" ]]; then
    [[ -f "$MARKER" && "$(cat "$MARKER")" == "$TARGET" ]] || fail '已有非本安装器管理的同名命令，未覆盖'
fi
PENDING=$(mktemp "$BIN_DIR/.clean-my-agent.XXXXXX")
cp -- "$TMP_INSTALL/clean-my-agent" "$PENDING"
chmod 755 "$PENDING"
mv -f -- "$PENDING" "$TARGET"
PENDING=""
printf '%s\n' "$TARGET" > "$MARKER"
if [[ "$CHANGE_PATH" == 1 ]]; then
    path_line="case \":\$PATH:\" in *:$(quote "$BIN_DIR"):*) ;; *) export PATH=$(quote "$BIN_DIR"):\$PATH ;; esac"
    for profile in "${PROFILES[@]}"; do
        mkdir -p -- "$(dirname "$profile")"
        if ! grep -Fq -- "$START" "$profile" 2>/dev/null; then
            printf '\n%s\n%s\n%s\n' "$START" "$path_line" "$END" >> "$profile"
        fi
    done
    fish_dir="${XDG_CONFIG_HOME:-$HOME/.config}/fish/conf.d"
    mkdir -p -- "$fish_dir"
    printf '%s\ncontains -- %s $PATH; or set -gx PATH %s $PATH\n%s\n' "$START" "$(quote "$BIN_DIR")" "$(quote "$BIN_DIR")" "$END" > "$fish_dir/clean-my-agent.fish"
fi
printf '已安装 %s\n' "$probe"
case ":$PATH:" in *":$BIN_DIR:"*) printf '%s\n' '运行：clean-my-agent';;
    *) printf '重新打开终端后运行：clean-my-agent\n当前终端立即使用：%s\n' "$(quote "$TARGET")";; esac
