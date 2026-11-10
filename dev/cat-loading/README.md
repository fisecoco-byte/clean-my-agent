# 胖英短翻书 Loading 预览

已选定的 Loading 动画及独立预览；扫描界面使用同一份字符图案，动画下不显示描述文字。

- `preview.html`：可直接打开的离线动画，支持暂停、重播；尊重系统的减少动态效果设置。
- `cat-loading.gif`：6 秒循环预览，由 72 个 12 fps 源帧生成；GIF 合并相邻静止帧并保留时长。
- `preview.py`：使用同一份字符帧的终端预览，不扫描、不清理任何文件。
- `poster.png` / `contact-sheet.png`：静态首帧和关键动作。

蓝灰色胖英短，圆脸、短耳朵、铜色眼睛和厚实身形。用放大镜扫读、眨眼、摆尾，随后抬爪翻过一页继续查看。

在仓库根目录运行终端预览，按 Ctrl+C 退出：

```sh
python3 dev/cat-loading/preview.py
```

重新生成（开发时需要 Node.js；GIF 渲染脚本使用 macOS 系统字体）：

```sh
npm --prefix dev/pie ci --ignore-scripts --no-audit --no-fund
node dev/pie/generate-cat-loading.mjs
node dev/pie/generate-loading.mjs
uv run --no-project --with pillow python dev/cat-loading/render-preview.py
```

字符图案为本项目原创，使用 ascii.rest 0.4.0 的 `Surface`、`stamp`、`line` 生成；其 MIT 许可见 `ascii.rest-LICENSE`。Python 预览不依赖 Node.js 或 ascii.rest。

运行时只使用预生成的字符帧和颜色，不需要 Node.js 或联网。扫描界面只保留真实数量进度和耗时；窄小窗口回退到单行扫描动画。
