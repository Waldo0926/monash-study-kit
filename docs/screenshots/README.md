# Documentation screenshots / 文档截图

`course-export.png` shows actual Study Kit 1.11.2 CLI output and the resulting ZIP listing, captured on 3 November 2026. All course data is synthetic. A missing attachment and an unauthorized Google link deliberately demonstrate incomplete exports. No Google consent or network sync is performed.

`course-export.png` 是 2026-11-03 使用 1.11.2 实际导出和 ZIP 文件列表生成的截图。课程资料全部为合成数据，故意保留缺失附件及未授权 Google 链接；未登录、未联网同步。

To reproduce the underlying export / 重现实际导出：

```sh
# Choose a new, empty scratch directory. Never use your real Study Kit home.
# 使用新的空临时目录，不要指向真实账号数据。
export MONASH_KIT_HOME="$(mktemp -d)"
uv run python scripts/demo_course_export.py
uv run monash export FIT1043 --format both --to ./exports --no-ask
```

Install Chrome, Edge or Brave for PDF output. The renderer may otherwise fall back to Markdown. The screenshot presents captured terminal text and the ZIP member list in a readable frame; it is not an additional product UI. `todo.png` remains a separate example of the todo command, rather than a picture of the exporter.

PDF 输出需要 Chrome、Edge 或 Brave，否则可能回退到 Markdown。截图仅将实际终端文字和 ZIP 清单排版，排版框不是产品界面。`todo.png` 仍是待办命令的独立示例，不代表导出功能。
