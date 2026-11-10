# Clean My Agent

在终端里查看 **Codex、Claude Code、T3 的主目录占用，以及其他 Agent 的已知缓存和旧版本**。工作树支持 Python、Rust、Java、Go、.NET、JS 等项目产物，区分目录大小、共享占用和单独删除预计释放空间。

[下载发布包](https://github.com/fisecoco-byte/clean-my-agent/releases/latest) · [安装与发布](docs/installation.md) · [图文使用手册](docs/user-guide.md) · [AI / MCP 接入](docs/agent-setup.md)

## 第一次运行

安装包自带 Python、终端界面和 MCP，无需准备开发环境。以下命令安装或更新到最新正式 Release。

**macOS / Linux / Ubuntu：**

```sh
curl -fsSL https://raw.githubusercontent.com/fisecoco-byte/clean-my-agent/main/install.sh | bash
```

**Windows PowerShell：**

```powershell
irm https://raw.githubusercontent.com/fisecoco-byte/clean-my-agent/main/install.ps1 | iex
```

安装器自动识别架构、校验下载并注册用户 `PATH`。macOS / Linux 首次安装后重新打开终端，在任意目录运行：

```sh
clean-my-agent
```

也可从 [Releases](https://github.com/fisecoco-byte/clean-my-agent/releases) 下载对应系统的压缩包，解压后运行包内安装脚本。安装、升级和卸载见[安装指南](docs/installation.md)。**Windows 原生版当前支持只读盘点**，暂不执行暂存、恢复或删除；macOS / Linux 保留完整清理流程。

**一个入口完成「看占用 → 查看清单 → 选择处理方式 → 确认」。** 按 `0 / 1 / 2 / 3` 选择全部 / Codex / Claude / T3，`↑ / ↓` 选类别，`Enter` 进入。子页面按 `q` 返回总览，首页按 `q` 退出。

![统一存储总览：按 Agent 查看占用，选择类别进入处理。虚构示例数据。](docs/dashboard.svg)

首页的工作树占用包含源码和依赖；进入后测量依赖、构建产物和工具缓存的目录大小、共享占用和预计释放空间；工作树列表按 `Enter` 查看详情、`p` 进入产物、`d` 核对整树删除。目录大小不等于可释放量，各行的释放估计不能相加。

只查看 Codex 时运行 `clean-my-agent --agent codex`。`dashboard`、`home` 和交互式 `scan` 也能进入总览。`--quick` 可跳过大目录并明确显示统计缺口；默认会统计完整主目录。

## 选对操作

| 总览中的入口 | 进入后可以做什么 |
| --- | --- |
| 会话与归档 | 查看 ID、标题和活跃时间；`a` 归档 / `d` 删除 → 空格勾选 → `x` 核对 |
| 工作树与依赖 | 选工作树查看详情；进入产物页勾选暂存，或单独确认原生删除整树 |
| 缓存与旧日志 | 空格勾选文件，`t` 核对并暂存 |
| Skill / MCP / Plugin | 复核不同版本、引用与原生回收建议，只读 |
| 版本与桌面资源 | 查看 Agent 缓存、旧版本、更新残留；候选按 `t` 核对并暂存 |
| 其他受保护数据 | 查看分类占用，保留 |
| 暂存区 | 空格勾选条目，`r` 恢复或 `d` 删除；输入 `DELETE` 后按 `Enter` 确认 |

**暂存释放 0 B。** 要释放空间，在总览进入「暂存区」，勾选条目后按 `d`，输入 `DELETE` 并按 `Enter` 确认永久清除。暂存内容可恢复，永久清除不可恢复。会话归档也不保证释放空间。工具不压缩数据。Codex 会话通过原生命令管理，Claude 会话仅供复核。工作树列表提供独立的整树删除入口：检查 Git 改动、进程、引用和忽略文件，输入 `DELETE` 确认后通过 Git 删除；此操作不进入暂存区。详见[工作树操作说明](docs/worktrees.md)。

依赖暂存前检查项目类型、Git 保护、工作树引用和运行状态；Node 依赖另查安装记录与安装后修改。旧版本保留活动版本和一个回退版本，运行中或无法确认的资源保留。完整规则见[资源与版本说明](docs/resources-and-versions.md)。

**已逐项补齐 Mole 产物表中的 34 类目录和 Agent 专用资源规则。** [完整对照表](docs/mole-reference.md)列出每项路径、清理条件和有意保留的状态数据。Skill / MCP / Plugin 多版本继续只读复核。

```sh
clean-my-agent deps                  # 多语言产物，w 工作树列表，Space 勾选产物，t 暂存
clean-my-agent resources             # Agent 缓存、旧版本、残留，t 暂存
clean-my-agent resources --agent codex
clean-my-agent extensions            # 扩展多版本，只读
```

## 在面板中完成操作

总览中的会话、依赖和清理页使用相同的表格、详情及确认弹窗。会话页展示全部匹配会话：用空格勾选，按 `x` 核对完整 ID、标题和操作清单。永久删除还需输入 `DELETE`。执行前会重新核实会话活动与保护规则，执行后展示每项结果并刷新列表。

![Reporter 勾选会话示例](docs/reporter-table.svg)

依赖页按空格勾选候选，`t` 列出待暂存清单，`Enter` 确认，`Esc` 取消。按 `v` 直接进入暂存区，空格勾选条目，`d` 删除或 `r` 恢复。同一批次未勾选的条目保留。操作后首页重新统计；只浏览时复用结果，也可按 `r` 主动刷新。

已有单项命令继续可用：`deps`、`resources`、`reporter`、`extensions`、`clean --details`、`trash`。脚本或 AI 使用 `dashboard --json` 获取主目录总览，使用 `scan --json` 获取含依赖测量的完整报告；`--plain`、管道和无交互终端不会等待按键。

## 让 AI 安装并使用

安装发布包后，可直接注册只读 MCP，按客户端选择一条：

```sh
codex mcp add clean-my-agent -- clean-my-agent mcp
claude mcp add --scope user clean-my-agent -- clean-my-agent mcp
```

把下面这句话发给**运行在本机**的 Codex 或 Claude Code 也可以：

> 按 https://github.com/fisecoco-byte/clean-my-agent 的安装说明安装发布包，并注册 clean-my-agent mcp。安装后调用 get_policy 验证，按 Codex、Claude Code 分别给我占用报告。不要暂存或删除任何文件。

源码开发者仍可使用旧的安装脚本（需要 Python 3.10+），按客户端选一条：

```sh
./scripts/install-agent.sh codex
./scripts/install-agent.sh claude
```

安装脚本在本项目的虚拟环境安装 MCP，并注册为当前用户的本地服务；不会执行扫描或清理。重新打开客户端后，Codex 可用 `$clean-my-agent`，Claude Code 可用 `/clean-my-agent`。有关权限、可调用工具、手工配置和卸载，见[Agent 接入指南](docs/agent-setup.md)。
