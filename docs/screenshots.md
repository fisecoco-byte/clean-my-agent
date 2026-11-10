# 历史 Python 版动态演示 / Legacy Python demos


以下为迁移前 v0.3.2 的 Textual 演示，保留供历史对照；当前 Rust 版见 [Rust 截图](rust-screenshots.md)。These are legacy demonstrations, not the current Rust interface.

- 生成时间 / Captured: `2026-11-10T16:02:44+08:00`
- 录制版本 / Captured version: `0.3.2`
- 来自实际 Textual 界面，数据为固定示例，不含真实用户路径或会话。
- Rendered by the actual Textual apps with fictional fixtures. No user storage was scanned and no native actions were executed.
- 中英文 README 共用三段循环 GIF：小猫扫描与 Agent 切换、依赖勾选与暂存复核、会话勾选与归档清单。
- Three looping GIFs cover the cat loading animation and agent filters, dependency selection and staging review, and session selection and archive review.
- 扫描进度为模拟数据；确认页只展示并取消。Loading progress is simulated; confirmation dialogs are only previewed and cancelled.

## 重新生成 / Regenerate

录制依赖 / Recording dependencies: Python UI extras (`pip install -e '.[ui]'`), `rsvg-convert` (librsvg), ImageMagick 7 (`magick`), and a CJK font.
这些仅用于制作文档，终端工具和发布安装包不需要它们。These are documentation tools, not runtime dependencies.

macOS: `brew install librsvg imagemagick`。Linux 可安装对应工具与中文字体，确保上述命令位于 PATH；Windows 可在 WSL 中录制。

在仓库根目录运行 / Run from the repository root:

```sh
.venv/bin/python dev/capture-readme.py
```

脚本调用当前 `DashboardApp`、`StorageApp` 和 `SessionApp`，通过实际按键 / 点击录制。
执行接口均为禁止操作的替身；修改产品界面后重新运行即可更新 GIF 和 SVG 静态备份。
The script records real key presses and clicks against the current apps with blocked mutation handlers, then renders SVG frames offline and encodes GIFs.
中间帧保存在忽略的 `reports/readme-gif/`；GIF 宽度为 1600 像素，停留 1–4 秒方便阅读，小猫动画为 80 毫秒 / 帧。
Intermediate frames stay in ignored `reports/readme-gif/`. GIFs are 1600 px wide, with 1–4 second reading pauses and 80 ms cat-animation frames.
画面中的采集时间 `2026-11-10T12:00:00+08:00` 属于示例数据，不是真实扫描时间。

| 动态演示 / GIF | 时长 / Duration | 大小 / Size | SHA-256 |
| --- | --- | --- | --- |
| [dashboard.gif](dashboard.gif) | 16.04s | 1401 KiB | `3e6625d19bd5cf1b5c458391abda46af481d2c810c5b9e19e10e8138fa28f97b` |
| [deps-table.gif](deps-table.gif) | 16.30s | 498 KiB | `cac6aeb77749a8004cf0b11a755ce9e5c6b5a68af4df5111498ce26155388452` |
| [reporter-table.gif](reporter-table.gif) | 16.50s | 471 KiB | `a15448be63b4d95418b539321e99966ae61883c2fea278d20b43a84556286d2a` |

## 静态备份 / Static alternatives

| 文件 / File | 终端尺寸 / Columns × rows | SHA-256 |
| --- | --- | --- |
| [dashboard.svg](dashboard.svg) | 120 × 34 | `151b03c9351ade55df2289634e9307900091a44ee688ce1f478cc14859c03b89` |
| [deps-table.svg](deps-table.svg) | 152 × 34 | `069b7fc537400c9ac9176c4cbe0e54c75072efe9b7a337e3293adad1168fc1de` |
| [reporter-table.svg](reporter-table.svg) | 156 × 34 | `538437b0e52a2eddaef464e74de90eb2eedce503ee609b315b1d91f57a6aab79` |
