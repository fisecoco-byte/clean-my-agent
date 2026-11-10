# 给 Codex 和 Claude Code 安装 Clean My Agent

[首页](../README.md) · [图文使用手册](user-guide.md) · [Skill 源文件](../skills/clean-my-agent/SKILL.md)

**MCP 提供可调用的本机工具，Skill 教 Agent 如何选工具和解释结果。** 默认安装只开放盘点和预览。服务使用标准输入输出（stdio），由客户端按需启动，不监听端口。扫描在运行服务的那台机器上进行；返回给客户端的路径、标题和 ID 可能出现在该客户端的模型上下文中。

## 给 AI 的安装指令

推荐使用[发布包安装](installation.md)，无需 Python 环境：

> 从 https://github.com/fisecoco-byte/clean-my-agent 的 Release 安装对应系统的程序，确认 clean-my-agent --version 可运行，再为当前客户端注册 clean-my-agent mcp，只读模式。保留已有同名配置。安装后列出 MCP 工具并真实调用 get_policy，不执行清理。

安装好全局命令后，按客户端选一条：

```sh
codex mcp add clean-my-agent -- clean-my-agent mcp
claude mcp add --scope user clean-my-agent -- clean-my-agent mcp
```

桌面客户端若尚未继承更新后的 PATH，可用安装器输出的可执行文件绝对路径替换命令名。发布压缩包内带有 `skills/clean-my-agent`，可完整复制到下表对应的 Skill 目录；不要覆盖已有同名目录。Windows 当前只提供只读 MCP。

## 源码开发安装

把本项目放到一个长期保留的目录，打开有本机终端权限的 Agent，把下面的话发给它：

> 在我提供的 Clean My Agent 源码目录检查 Rust 工具链和当前客户端。为正在使用的客户端运行 scripts/install-agent.sh codex 或 scripts/install-agent.sh claude，安装用户级只读 MCP 和 Skill。保留同名已有配置。安装后验证能列出工具，并真实调用 get_policy；如果当前会话还未加载，告诉我如何重新打开验证。不要执行清理。

若拿到的是源码包，先在希望保留工具的位置解压并进入目录（已在源码目录可跳过）：

```sh
tar -xzf /实际路径/clean_my_agent-0.3.1.tar.gz
cd clean_my_agent-0.3.1
```

需要 macOS/Linux、Rust 工具链、对应的 `codex` 或 `claude` CLI。首次构建需要能访问 crates.io；以后运行不依赖远程 MCP 服务。在**本项目根目录**任选一条：

```sh
./scripts/install-agent.sh codex
./scripts/install-agent.sh claude
```

脚本可从含空格的路径运行。它使用 Cargo 构建 Rust 可执行文件，再注册绝对路径的 `target/release/clean-my-agent mcp`，并建立 Skill 的符号链接：

| 客户端 | MCP 注册范围 | Skill 位置 |
| --- | --- | --- |
| Codex | 当前用户的 Codex 配置 | `~/.agents/skills/clean-my-agent` |
| Claude Code | `--scope user` | `${CLAUDE_CONFIG_DIR:-~/.claude}/skills/clean-my-agent` |

重复安装会保留已有的同名 MCP（包括原权限）；遇到同名且不是本工具链接的 Skill 会停止。新装默认只读；若脚本提示已有配置，请核对启动命令和参数。**保留源码目录及 target/release 中的可执行文件**，移动后应重新注册路径。

## 验证连接并开始使用

1. 运行 `codex mcp get clean-my-agent` 或 `claude mcp get clean-my-agent`，核对 stdio 命令的绝对路径；默认参数中没有 `--allow-stage`。
2. 新开一个客户端会话。Codex 输入 `$clean-my-agent`，Claude Code 输入 `/clean-my-agent`。要求「用 MCP 调用 get_policy，告诉我当前策略和三类保留天数」。应收到实际的 `profile` 和 `retention_days`；仅看到配置名称并不能证明调用成功。
3. 试试「仅列出 Codex 中预计释放空间最多的五个依赖候选，同时告诉我目录大小和共享量」，或「列出 Claude Code 十天未活跃、建议复核的会话和标题」。

## 工具速查

客户端可能会给工具名加上服务前缀；下表使用 MCP 公布的原名。容量单位为字节，缺少精确值时返回 `null`。报告中 `errors` 非空表示存在不完整结果；不要把部分成功当作全部完成。

| 默认只读工具 | 参数示例 | 返回什么 |
| --- | --- | --- |
| `get_policy` | `{}` | 生效策略、保护路径、会话阈值 |
| `scan_usage` | `{"agent":"codex","full":false}` | 分类占用、符合策略的容量、未展开目录数；完整模式另有最大的目录组 |
| `inspect_dependencies` | `{"candidates_only":true,"limit":5}` | 按单独预计释放量排序的依赖、共享量、保留原因、引用来源；可传一个候选的绝对 `path` |
| `resource_inventory` | `{"agent":"cursor","full":true,"limit":20}` | Agent 缓存、旧版本、残留及暂存资格 |
| `extension_inventory` | `{"agent":"all","full":false,"limit":20}` | Skill/MCP/Plugin 多版本、已知引用及原生回收建议；不修改扩展 |
| `session_inventory` | `{"agent":"claude","archive_days":10,"delete_days":20,"limit":5}` | 按 agent 的活跃分布和复核清单，含 ID 与已有标题；不执行归档 |
| `preview_clean` | `{"agent":"codex","days":60,"limit":10}` | 缓存与旧日志的候选总数、容量与部分路径 |
| `list_batches` | `{}` 或 `{"batch":"实际批次号"}` | 最近批次，或指定批次内的原路径、条目数、清单大小 |

会话与扩展的 `agent` 可选 `codex`、`claude`、`all`；`scan_usage` 还接受 `t3`，`resource_inventory` 接受规则表中的各 Agent。接受 `limit` 的工具允许 1–100，限制返回行数，不缩小文件系统核查范围。首次先快扫；完整容量或工作树引用核查可能需要数分钟。如果客户端超时，可在终端运行 `./clean-my-agent deps --json > deps.json`，用 `--from-report` 重看快照。写操作超时后先查看暂存批次，不自动重试。

产物报告覆盖多语言，有 `installation`、`runtime` 与工作树完整占用，检查不完整时保留。`scan_usage` 新增只读资源与扩展摘要，各最多 20 条并给出截断标记；原有 `bytes` 是主目录小计，不要把重叠资源再次相加。扩展目录没有新增写接口，详见[资源与版本说明](resources-and-versions.md)。

## 按需开放暂存和还原

若你希望 Agent 可以修改本机文件，可以为**同一个本地服务**显式增加启动参数 `--allow-stage`，再重启连接。它额外注册以下工具，原有清理策略和工作树引用保护继续生效：

| 工具 | 范围 |
| --- | --- |
| `stage_clean(agent, days?)` | 仅限指定一个 agent；重新检查并暂存全部匹配的缓存及日志，包括预览明细上限之外的项 |
| `stage_dependency(path)` | 一次一个已停用项目产物的绝对路径；重新核实类型、引用和可重建条件 |
| `stage_resource(path)` | 一次一个已确认的 Agent 资源绝对路径；复查活动版本、年龄、进程和锁 |
| `restore_batch(batch)` | 还原指定批次；不会覆盖同名新文件 |

先预览并核对完整操作范围，暂存后记录返回的批次号。**暂存不释放空间**；MCP 不提供永久清除、压缩、会话归档或删除接口。需要真正释放空间，使用 CLI 先 `./clean-my-agent purge BATCH` 预览，再自行决定是否加 `--apply`。重新启用只读模式只需移除服务启动参数并重启。

人直接使用终端时，`reporter` 支持勾选 Codex 会话、确认后调用原生命令；`deps` 和 `resources` 支持选中候选后按 `t` 暂存。这些界面操作与 MCP 的权限开关分开，详见[图文手册](user-guide.md)。

## 手动注册或接入其他客户端

不使用脚本时，在项目根目录安装并定位可执行文件：

```sh
cargo build --release --locked
server="$(pwd -P)/target/release/clean-my-agent"
```

按当前客户端选一条注册命令，再把项目中的 `skills/clean-my-agent` 链接到上表的 Skill 位置。不要覆盖其他同名 Skill。

```sh
codex mcp add clean-my-agent -- "$server" mcp
claude mcp add --scope user --transport stdio clean-my-agent -- "$server" mcp
```

若在全新注册时就要开放暂存，在所选命令的 `"$server" mcp` 后加 `--allow-stage`。切换现有配置时，先核对是自己的该项，再用下节命令移除 MCP 注册并重新添加；不必移除 Skill。其他支持 stdio 的客户端可使用以下片段（替换为本机真实绝对路径，配置键以客户端为准）：

```json
{
  "mcpServers": {
    "clean-my-agent": {
      "command": "/absolute/path/to/clean-my-agent/target/release/clean-my-agent",
      "args": ["mcp"]
    }
  }
}
```

客户端的服务进程可能没有继承交互 shell 的环境。如果使用自定义数据根，请在该服务的环境配置中指定与 CLI 相同的 `CODEX_HOME`、`CLAUDE_CONFIG_DIR`、`XDG_CONFIG_HOME`、`XDG_STATE_HOME` 等。恢复批次时尤其要确保路径一致。

## 取消接入

只运行对应客户端的命令：

```sh
codex mcp remove clean-my-agent
# 或
claude mcp remove --scope user clean-my-agent
```

再移除安装脚本建立的 Skill **链接**。例如 Codex 可用 `test -L "$HOME/.agents/skills/clean-my-agent" && unlink "$HOME/.agents/skills/clean-my-agent"`；Claude Code 对 `"${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills/clean-my-agent"` 执行相同操作。移除接入不会动数据目录或任何暂存批次。

参考：[MCP 官方 Rust SDK](https://github.com/modelcontextprotocol/rust-sdk)、[Codex Skills 的发现位置](https://developers.openai.com/codex/skills/)、[Claude Code 的 MCP 注册范围](https://code.claude.com/docs/en/mcp)、[Claude Code Skills](https://code.claude.com/docs/en/skills)。
