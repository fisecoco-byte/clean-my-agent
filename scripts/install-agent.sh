#!/bin/sh
# Run from a trusted source tree; clients launch the server at an absolute path.
set -eu
case "${1:-}" in
    -h|--help) echo '用法：scripts/install-agent.sh codex|claude  （安装用户级只读 MCP 和 Skill）'; exit 0 ;;
    codex|claude) client=$1 ;;
    *) echo '用法：scripts/install-agent.sh codex|claude' >&2; exit 2 ;;
esac
[ "$#" -eq 1 ] || { echo '只接受一个客户端名称' >&2; exit 2; }
command -v "$client" >/dev/null || { echo "请先安装 $client CLI 并加入 PATH" >&2; exit 1; }
repo=$(CDPATH='' cd -P "$(dirname "$0")/.." && pwd -P)
src="$repo/skills/clean-my-agent"
case "$client" in
    codex) dir="$HOME/.agents/skills" ;;
    claude) dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills" ;;
esac
dest="$dir/clean-my-agent"
if [ -e "$dest" ] || [ -L "$dest" ]; then
    [ -L "$dest" ] && [ "$(readlink "$dest")" = "$src" ] || {
        echo "同名 Skill 已存在，未覆盖：$dest" >&2; exit 1; }
fi
cargo=${CARGO:-cargo}
if ! command -v "$cargo" >/dev/null 2>&1 && [ -z "${CARGO:-}" ] && [ -x "$HOME/.cargo/bin/cargo" ]; then
    cargo="$HOME/.cargo/bin/cargo"
fi
(cd "$repo" && "$cargo" build --release --locked)
server="$repo/target/release/clean-my-agent"
if "$client" mcp get clean-my-agent >/dev/null 2>&1; then
    echo "已有同名 MCP，已保留原启动命令和权限。请用 $client mcp get clean-my-agent 核对路径：$server"
elif [ "$client" = codex ]; then
    codex mcp add clean-my-agent -- "$server" mcp
else
    claude mcp add --scope user --transport stdio clean-my-agent -- "$server" mcp
fi
mkdir -p "$dir"
[ -L "$dest" ] || ln -s "$src" "$dest"
echo "Skill：${dest}。新开会话，让 Agent 调用 get_policy 验证连接。"
