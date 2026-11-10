# Mole Agent 清理能力与实现对照

[返回首页](../README.md) · [操作与保护规则](resources-and-versions.md)

核对日期：2026-11-09；本工具版本：0.3.0。参考 Mole 固定提交 [`323e217`](https://github.com/tw93/Mole/commit/323e21713131c67d2d920862ec0dd572c5b20f76)，范围是 **Agent 资源和项目产物**，不包含 Mole 的系统、Docker、数据库等整机清理功能。

以下“已支持”表示已接入扫描、保留判断、手动暂存、恢复和永久清除前复查，**不表示扫描到就会删除**。macOS / Linux 开放修改；Windows 原生版保持只读。桌面资源按 Mole 已核实的 macOS 路径识别，不猜测其他系统或自定义安装路径。

## 1. Mole 的 34 种项目产物

来源：[产物目录与扫描规则](https://github.com/tw93/Mole/blob/323e21713131c67d2d920862ec0dd572c5b20f76/lib/clean/purge_shared.sh)、[项目验证与保护](https://github.com/tw93/Mole/blob/323e21713131c67d2d920862ec0dd572c5b20f76/lib/clean/project.sh)。这些名称是发现规则，不是单凭名称删除的许可。

| 生态 / 用途 | Mole 发现的产物 | Clean My Agent 0.2.0 |
| --- | --- | --- |
| JS / TS 依赖 | `node_modules` | 已支持；保留 npm / pnpm / Yarn 安装记录与安装后修改检查 |
| Web 框架 | `.next`、`.nuxt`、`.output`、`.angular`、`.svelte-kit`、`.astro`、`.expo` | 已支持；要求对应项目清单 |
| JS 工具缓存 | `.turbo`、`.parcel-cache` | 已支持 |
| Python 环境 | `venv`、`.venv`、`.tox`、`.nox` | 已支持；前两项还要求 `pyvenv.cfg` |
| Python 缓存 | `__pycache__`、`.pytest_cache`、`.mypy_cache`、`.ruff_cache` | 已支持；字节码目录含其他文件时保留 |
| Rust / Maven | `target` | 已支持；要求 Cargo 或 Maven 清单 |
| Gradle / Android | `.gradle`、`.cxx` | 已支持；要求 Gradle / Maven / CMake 对应证据 |
| 通用构建与测试输出 | `build`、`dist`、`coverage` | 已支持 JS、Python、JVM、Dart、C/C++、Ruby、Go、Rust、Swift 项目上下文 |
| PHP / Composer | `vendor` | 已支持 Composer 清单、锁文件及安装记录；Go / Ruby 的 vendor 保留 |
| .NET | `bin`、`obj` | 已支持；要求 .NET 项目文件，`bin` 还要求 Debug / Release 子目录；普通 bin 保留 |
| Dart / Flutter | `.dart_tool` | 已支持 |
| Zig | `.zig-cache`、`zig-out` | 已支持 |
| Apple | `DerivedData`、`Pods`、`.build` | 已支持；Pods 要求两份锁记录一致；SwiftPM 依赖与状态保留，展开可独立清理的编译输出 |
| Terragrunt | `.terragrunt-cache` | 已支持；含持久状态或密钥时保留 |

另外支持有效 `CACHEDIR.TAG` 声明的缓存。SwiftPM 编译输出包括 `.build` 内的 `out`、`index-build`、`arm64-apple-macosx`、`x86_64-apple-macosx`、`aarch64-unknown-linux-gnu`、`x86_64-unknown-linux-gnu`；`checkouts`、`repositories`、`artifacts`、`workspace-state.json` 不随编译输出清理。

扫描 `$CODEX_HOME`、`$CLAUDE_CONFIG_DIR`、`$T3_HOME` 内的 `worktrees`、`.chatgpt-projects`、`projects`，支持仓库内子项目。Git 登记的其他工作树用于检查引用，不因此进入清理范围。0.3.0 额外提供[工作树选择和原生删除](worktrees.md)：即使没有已知产物，也可按 `w` 查看完整占用；整树删除独立确认，不纳入常规清理。不会自动扫描所有用户项目或全局语言包仓库。

## 2. Mole 的 Agent 专用清理

来源：[Agent 安装、缓存、更新残留与保护规则](https://github.com/tw93/Mole/blob/323e21713131c67d2d920862ec0dd572c5b20f76/lib/clean/dev.sh)。以下项目都已接入 `resources`，路径前缀默认是用户主目录。

| Agent / 资源 | 识别位置与范围 | 已实现的保留判断 |
| --- | --- | --- |
| Claude Code 旧版本 | `.local/share/claude/versions` | 当前启动链接指向版本 + 1 个回退版本；活动版本不明确时全部保留 |
| Cursor Agent 旧版本 | `.local/share/cursor-agent/versions` | 同上 |
| GitHub Copilot CLI 旧版本 | `.copilot/pkg/universal` | 同上 |
| Claude Desktop 内置 SDK | `Library/Application Support/Claude/{claude-code,claude-code-vm}` | `.sdk-version` 活动版本、1 个回退版本及运行状态 |
| Codex app-server daemon | `.codex/packages/app-server-daemon/releases` | `current`、`auto-update-version`、1 个回退版本；持有 `install.lock` 执行暂存/清除 |
| Codex 浏览器缓存 | `Library/Caches/Codex` 中三个已知配置下的 `Cache` / `Code Cache` | 应用与相关进程停止；其他配置、登录信息保留 |
| Codex Sparkle 更新残留 | `Library/Caches/com.openai.codex/org.sparkle-project.Sparkle/Installation` | 比已安装版本新的更新保留；至少 30 天无改动；应用/更新器停止 |
| Codex Crashpad | `Library/Application Support/Codex/Crashpad/pending` 直属普通文件 | 至少 30 天；应用/上传进程停止、文件未打开 |
| Codex runtime 残留 | `.cache/codex-runtimes` | 完整 Node / Python 运行环境保留；仅识别临时、未完成或同时缺少运行清单和依赖的目录 |
| Codex marketplace staging | `.codex/.tmp/bundled-marketplaces`、`.codex/.tmp/marketplaces/.staging` | 仅 `openai-bundled.staging-`、`marketplace-upgrade-`、`marketplace-add-` 前缀；完成的市场保留 |
| Claude Desktop 缓存 | `Library/Application Support/Claude` 的已知 Electron 缓存叶目录、`sentry` | 应用停止；数据库、会话和配置保留 |
| Antigravity / Gemini | macOS Electron 缓存、`.gemini/antigravity-browser-profile` 内指定 Chromium 缓存 | 进程停止；`.gemini/tmp` 会话检查点保留 |
| T3 Code | `Library/Application Support/{t3code-v2,T3 Code (Alpha)}/Partitions/t3code-preview-*` 的 Cache / Code Cache | 只处理预览缓存；`.t3` 会话和登录状态保留 |
| Qoder | macOS Electron 缓存、CachedData、CachedExtensionVSIXs、logs | 应用停止；扩展安装与用户状态保留 |
| Filo | `Library/Application Support/Filo/production` 的 Electron 缓存 | 应用停止 |
| OpenCode | `.cache/opencode` | 仅缓存；不处理其他位置的快照、会话、诊断 JSONL |
| Cursor 日志 | `.local/share/cursor-agent` 内旧 `*.log`，跳过 versions | 保留期、进程与打开文件检查 |
| Chrome DevTools MCP | `.cache/chrome-devtools-mcp/chrome-profile` 内指定 Chromium 缓存 | 不删除整个 profile；Cookies、Local Storage、IndexedDB 等保留 |
| agent-browser 旧 Chrome | `.agent-browser/browsers/chrome-*` | 保留程序按名称选中的完整安装、未完成安装、用户配置和环境变量引用；暂存后新增引用也阻止清除 |

Electron 固定缓存叶目录包括 Cache、Code Cache、GPUCache、DawnGraphiteCache、DawnWebGPUCache。浏览器配置只处理源码明确列出的缓存叶目录及 CacheStorage，不包含全部 Service Worker 状态。

## 3. 有意保留的内容

| 内容 | 当前处理方式 |
| --- | --- |
| 源码、会话、数据库、凭据、历史 | 不纳入常规清理；Codex 会话仍通过原生命令管理 |
| 附加工作树 | 额外的显式选择入口，通过检查并确认后原生 Git 删除；主工作区保留 |
| 已安装的 Skill / MCP / Plugin 不同版本 | `extensions` 检测多版本、已知引用和原生回收提示；不会仅因版本较旧就删除 |
| Playwright 浏览器版本 | 不进入清理候选；Mole 也明确保护 |
| Codex 完整 runtime、已完成 marketplace | 保留 |
| 未被上游确认格式的插件安装暂存目录 | 只读盘点 |
| 全局 Go、Maven、NuGet、pnpm 等包存储 | 本次不扩展到全局语言环境；它们不是工作树产物，也不等同 Agent 缓存 |

与 Mole 的行为差异：本工具先暂存，暂存本身释放 **0 B**，再由用户按批次永久清除。默认项目产物 7 天、缓存/日志 30 天；旧版本也遵守保留期。项目还要求 Git 忽略、清单被跟踪、目录没有已跟踪文件、无已知工作树引用、运行状态可确认。进程检查和磁盘测量都是时点观测，不承诺原子快照或任意路径的完整引用图。

## 4. 使用入口

```sh
clean-my-agent                         # 总览，选择工作树或版本与桌面资源
clean-my-agent deps --agent codex       # 多语言项目产物；w 查看工作树完整占用
clean-my-agent resources               # Agent 缓存、旧版本、残留；t 暂存
clean-my-agent resources --agent cursor
clean-my-agent extensions              # Skill / MCP / Plugin 版本复核，只读
```

候选选择后仍需确认。`resources` 和 `deps` 均可输出 `--json`，所有写入口会再次核查条件。详细恢复/清除步骤见[使用手册](user-guide.md)。
