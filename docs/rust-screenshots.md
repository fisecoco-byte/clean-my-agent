# Rust 界面截图 / Rust terminal screenshots

录制版本 / Version: `0.4.1` · 2026-11-10T17:04:33+00:00

从原生 Rust 程序的真实终端输出导出，保留终端网格与颜色。全部数据来自临时 HOME 内的虚构项目和会话，不扫描用户数据。
Captured from the native Rust executable in a real PTY, preserving its grid and colors. All fixtures live in a temporary HOME.

录制过程同时验证勾选、清理入口、暂存、恢复和永久清除；文件操作只针对临时测试数据。
The recording also tests selection, cleanup actions, staging, restore and purge, exclusively on temporary fixtures.

重新生成 / Regenerate (macOS / Linux):

```sh
cargo build --release --locked
python3 -m venv .venv
.venv/bin/python -m pip install pyte
.venv/bin/python dev/capture-rust-readme.py
```

Python 和 pyte 仅用于开发录制，发布程序不需要它们。Python and pyte are capture tools, not runtime dependencies.

| Screenshot | SHA-256 |
| --- | --- |
| [dashboard](rust-dashboard.svg) | `6efd3ee0daf55d75e1d09095cba6b4a670614d4ab4eead62b007c21b3d88b7c7` |
| [workspaces](rust-workspaces.svg) | `83c920419964ba3caeb5b788c0d77a2ea351d975cb854e1ef71c61b09c09b852` |
| [deps](rust-deps.svg) | `1da0eefcfd63c0b6e3e477c502226f5929c3b611109e0fa4b8c917a3899e6ea8` |
| [sessions](rust-sessions.svg) | `eb3048d0321ccc2735b1ec89f61aea9d0098c199435cec7a83d8beed14264b22` |
