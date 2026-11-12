# 安装与发布

发布仓库：[fisecoco-byte/clean-my-agent](https://github.com/fisecoco-byte/clean-my-agent)。在线安装默认获取最新正式 Release，也可指定版本。

## 一行安装

macOS / Linux / Ubuntu：

```sh
curl -fsSL https://raw.githubusercontent.com/fisecoco-byte/clean-my-agent/main/install.sh | bash
```

Windows PowerShell：

```powershell
irm https://raw.githubusercontent.com/fisecoco-byte/clean-my-agent/main/install.ps1 | iex
```

安装器下载匹配平台的发布包，核对 `SHA256SUMS`，执行版本检查后替换程序。失败时保留已有程序，不运行扫描或清理。不需要 Python、pip、Homebrew、Node.js 或管理员权限。

安装后在任意目录执行：

```sh
clean-my-agent
clean-my-agent --version
clean-my-agent --agent codex
```

macOS / Linux 首次安装后重新打开终端，让 `PATH` 生效。安装器无法改写调用它的父 shell 环境，也会打印当前终端可立即使用的完整命令。PowerShell 在线安装会同步更新当前进程和用户 `PATH`，其他已打开的终端需重开。

## 下载后安装

从 [Releases](https://github.com/fisecoco-byte/clean-my-agent/releases/latest) 选择对应附件，解压并进入 `clean-my-agent` 文件夹：

| 平台 | 包名后缀 | 运行安装脚本 |
| --- | --- | --- |
| macOS Apple Silicon | `macos-arm64.tar.gz` | `bash install.sh` |
| macOS Intel | `macos-x64.tar.gz` | `bash install.sh` |
| Linux / Ubuntu x64 | `linux-x64.tar.gz` | `bash install.sh` |
| Linux / Ubuntu ARM64 | `linux-arm64.tar.gz` | `bash install.sh` |
| Windows x64 | `windows-x64.zip` | `powershell -ExecutionPolicy Bypass -File .\install.ps1` |

包内安装脚本识别本地程序，安装阶段无需联网。也可以直接执行包内程序进行便携使用。PowerShell 的 `Bypass` 参数仅作用于这次安装进程，不改系统执行策略。

Linux 包基于 Ubuntu 22.04 构建，需要 glibc 2.35+，适用于 Ubuntu 22.04 / 24.04 等系统；Alpine / musl 暂不支持。macOS 构建验证环境为 Apple Silicon macOS 15 和 Intel macOS 15。Windows 验证目标为 Windows x64，ARM64 暂不提供原生包。

当前发布流程未配置 Apple 公证或 Windows Authenticode 签名。直接从浏览器下载时，系统可能提示未识别开发者；校验值用于核对发布文件，不等同于代码签名。正式分发需由仓库维护者提供签名证书后接入签名步骤。

## 安装位置与 PATH

| 系统 | 程序位置 | PATH 注册 |
| --- | --- | --- |
| macOS / Linux | `~/.local/bin/clean-my-agent` | Bash / Zsh 启动文件中的标记区块，以及 Fish `conf.d/clean-my-agent.fish` |
| Windows | `%LOCALAPPDATA%\Programs\clean-my-agent\clean-my-agent.exe` | 当前用户环境变量 `Path` |

这是当前用户的全局命令，不修改整机环境变量。重复安装不追加重复条目；已存在且不属于本安装器的同名命令不会被覆盖。可以用 `--bin-dir /绝对路径 --no-path` 或 PowerShell `-InstallDir PATH -NoPath` 自行管理路径。

Unix 自定义 `ZDOTDIR`、`XDG_CONFIG_HOME` 时，安装与卸载应使用相同值。卸载只移除本工具程序与管理的 PATH 区块，保留策略、报告和暂存数据。

## 更新、指定版本与卸载

再次运行在线安装命令即更新到最新正式 Release。指定版本：

```sh
curl -fsSL https://raw.githubusercontent.com/fisecoco-byte/clean-my-agent/main/install.sh | bash -s -- --version v0.4.2
```

Unix 卸载：

```sh
curl -fsSL https://raw.githubusercontent.com/fisecoco-byte/clean-my-agent/main/install.sh | bash -s -- --uninstall
```

Windows 先下载脚本，再执行更新参数或卸载：

```powershell
irm https://raw.githubusercontent.com/fisecoco-byte/clean-my-agent/main/install.ps1 -OutFile install.ps1
.\install.ps1 -Version v0.4.2
# 卸载时运行：
.\install.ps1 -Uninstall
```

## 平台功能边界

macOS / Linux 的暂存与恢复继续使用原有文件描述符、锁和目录身份检查。依赖清理仍需本机 Git 和 `lsof`；缺少工具或检查不完整时保留。

Windows 原生包提供总览、会话 ID / 标题、资源与扩展版本、依赖的只读报告以及只读 MCP。文件分配量由 Windows API 读取，不含文件系统元数据；共享量、预计释放和完整工作树引用检查尚未支持，均标为未知或保留。符号链接、junction 和其他重解析点不跟随。**不开放暂存、恢复、永久清除、会话归档 / 删除和策略写入**。不会用宽松的路径操作代替原有清理保证。

普通文件读取 [FILE_STANDARD_INFO.AllocationSize](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_standard_info)；压缩或稀疏文件读取 [GetCompressedFileSizeW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-getcompressedfilesizew)。不以文件逻辑长度冒充分配量。

## 向 GitHub 发布

仓库上传到 `fisecoco-byte/clean-my-agent` 后：

1. 保留 `.github/workflows/release.yml`，启用 Actions。
2. 在 `Cargo.toml` 设置版本号；程序、打包元数据和发布标签使用同一版本。
   同时添加对应的 `docs/releases/vX.Y.Z.md` 发布说明。
3. 推送与版本一致的标签，例如：

   ```sh
   git tag v0.4.2
   git push origin v0.4.2
   ```

4. Actions 在五个对应系统 / 架构上构建，自带运行环境；完成测试后发布 Release、安装脚本、五个安装包和 `SHA256SUMS`。任一构建失败都不会进入发布步骤。

也可在 Actions 手动运行构建，默认只生成 workflow artifacts；勾选 `publish` 后，五个平台验证通过才会创建版本标签与正式 Release。发布后还会在五个平台验证公开的在线安装入口。不要把本地 `.venv`、`build`、`dist` 或私人 `reports` 上传到源码仓库；它们已在 `.gitignore` 中排除。

本地只构建当前平台：

```sh
cargo test --locked
cargo build --release --locked
python3 scripts/build-release.py --target macos-arm64
```

有 Docker 时，也可以在 Mac 上构建并验证 Linux 包：

```sh
bash scripts/build-linux-container.sh linux-arm64
bash scripts/build-linux-container.sh linux-x64
```

本地容器使用 Rust / Debian Bookworm 构建镜像（glibc 2.36+）；正式发布仍使用上方的 Ubuntu 22.04 CI 环境（glibc 2.35+），完成回归测试和可执行文件检查后把安装包写入 `dist/`，运行结束自动移除构建容器。Intel macOS 和 Windows 包仍应在对应系统构建。

安装入口参考 [Mole 的脚本安装](https://github.com/tw93/mole/blob/main/install.sh)。程序由 Cargo 编译为原生可执行文件，内嵌终端动画、HTML 模板和 MCP；不打包 Python 解释器。发布压缩包附有 `BUILD_INFO.json`，可用 `clean-my-agent --build-info` 核对版本、目标平台和源码摘要。打包辅助脚本需要 Python 3.11+，使用和安装程序不需要 Python。

## 源码开发安装

源码开发需要 Rust 工具链：

```sh
cargo build --release --locked
cargo test --locked
./clean-my-agent
```

Windows 使用相同的 Cargo 构建命令，再用 `target\release\clean-my-agent.exe` 启动。发布包用户无需执行这些步骤。
