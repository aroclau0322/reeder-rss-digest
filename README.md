# RSS + Reeder DeepSeek 评分工具

这个小工具会读取 RSS/Atom 订阅源、OPML 文件，或“每行一个订阅源 URL”的文本文件，然后用 DeepSeek API 给文章打分、优化标题，并输出：

- `output/high_score.xml`：高分文章 RSS，可作为精选源使用。
- `output/top_articles.md`：适合直接阅读的高分清单。
- `output/all_articles.md`：所有候选文章的评分与 DeepSeek 总结，适合复盘。
- `output/reeder_high_score_sources.opml`：出现高分文章的来源集合，可导入 Reeder。
- `output/scored_items.json`：完整评分结果，方便后续二次处理。

## 使用方式

先准备 DeepSeek API Key：

```bash
export DEEPSEEK_API_KEY="你的 key"
```

也可以在当前目录新建 `.env`：

```bash
DEEPSEEK_API_KEY=你的 key
DEEPSEEK_MODEL=deepseek-v4-flash
```

单个 RSS：

```bash
python3 rss_reeder_ranker.py "https://example.com/feed.xml"
```

OPML 文件：

```bash
python3 rss_reeder_ranker.py subscriptions.opml
```

每行一个订阅源的文本文件：

```bash
python3 rss_reeder_ranker.py feeds.txt
```

带自己的评分偏好：

```bash
python3 rss_reeder_ranker.py subscriptions.opml \
  --rubric-file scoring_rubric.md \
  --min-score 80 \
  --limit 30
```

抓取每篇原文页面，让 DeepSeek 同时评分和总结：

```bash
python3 rss_reeder_ranker.py subscriptions.opml \
  --fetch-pages \
  --min-score 75
```

页面抓取会优先提取文章正文，并为每篇文章标记正文状态：`full`（全文）、
`partial`（正文截断）、`paywalled`（付费预览）或 `rss_only`（仅 RSS）。
DeepSeek 会同时输出评分置信度。公开站点的 JSON 不包含抓取到的正文，只保留状态、评分和总结。

只更新某个时间点之后的新文章：

```bash
python3 rss_reeder_ranker.py subscriptions.opml \
  --fetch-pages \
  --since 2026-08-03T12:00:00+00:00
```

更新 `daily_sources.json` 中登记的所有源：

```bash
python3 update_daily.py
```

默认会从每个订阅源上次成功更新的时间继续抓取；首次运行回看 48 小时。结果按链接去重，
保留最近 3 天，并额外生成一个统一的“近 3 日高分信息源”。
本地统一订阅地址仍由原来的发布目录提供；云端会使用站点根目录下的 `high_score.xml`。

## 不依赖 Mac 的云端更新

项目包含 `.github/workflows/publish.yml`，可由 GitHub Actions 每天上海时间 20:17 自动运行，
并在 23:17 补偿更新一次，
再通过 GitHub Pages 提供长期在线的 RSS 地址。

仓库上线后需要完成两项设置：

1. 在仓库的 Actions secrets 中添加 `DEEPSEEK_API_KEY`。
2. 在 Pages 设置中选择 GitHub Actions 作为发布来源。

完成后，Reeder 的统一订阅地址为：

```text
https://你的用户名.github.io/仓库名/high_score.xml
```

也可以在 Actions 页面手动运行一次“更新并发布 Reeder RSS”做首次验证。云端缺少 API Key 时任务会直接停止，避免用本地规则结果覆盖正式订阅。

只看某些链接路径的频道，例如 Digitaling 只看文章和项目：

```bash
python3 rss_reeder_ranker.py "http://www.digitaling.com/rss" \
  --include-url-path /articles/ \
  --include-url-path /projects/ \
  --fetch-pages
```

如果暂时没有 API Key，也可以先运行；工具会用本地规则预览流程，但最终筛选建议还是以 DeepSeek 评分为准。

## 给 Reeder 用

推荐流程：

1. 从 Reeder 导出 OPML，或准备一个订阅源列表。
2. 运行本工具生成 `output/top_articles.md` 和 `output/reeder_high_score_sources.opml`。
3. 把 `reeder_high_score_sources.opml` 导入 Reeder，得到一组更精简的高质量来源。
4. 如果你有自己的静态托管位置，也可以把 `high_score.xml` 放上去，作为一个“每日精选 RSS”订阅。

## DeepSeek 配置

默认模型是 `deepseek-v4-flash`。如需更强推理，可以改模型：

```bash
export DEEPSEEK_MODEL="deepseek-v4-pro"
```

也可以在命令里指定：

```bash
python3 rss_reeder_ranker.py subscriptions.opml --model deepseek-v4-pro
```
