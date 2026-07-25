#!/usr/bin/env python3
"""Update configured feeds and build a cloud-hostable Reeder RSS site."""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import shutil
import subprocess
import sys
import urllib.parse
from pathlib import Path

from rss_reeder_ranker import (
    FeedItem,
    ScoredItem,
    load_dotenv,
    write_json,
    write_markdown,
    write_opml,
    write_rss,
)


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "daily_sources.json"
CACHE_DIR = ROOT / "cache" / "feeds"
LOG_DIR = ROOT / "logs"
OUTPUT_DIR = ROOT / "output"
DEFAULT_PUBLISH_ROOT = Path("/Users/aroc/Public/reeder-rss-root")
DAILY_DIGEST_DIR = OUTPUT_DIR / "daily_digest"
DEFAULT_MIN_SCORE = 75
DEFAULT_DIGEST_LIMIT = 50
SHANGHAI_TIMEZONE = "Asia/Shanghai"


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )


def download_feed(name: str, url: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    output = CACHE_DIR / f"{name}.xml"
    result = run(
        [
            "curl",
            "-fsSL",
            "--retry",
            "2",
            "--connect-timeout",
            "10",
            "--max-time",
            "40",
            "-A",
            "rss-reeder-ranker/1.0",
            "-o",
            str(output),
            url,
        ]
    )
    if result.returncode != 0:
        raise RuntimeError(f"下载失败：{url}\n{result.stdout}")
    return output


def source_output_dir(source: dict[str, object]) -> Path:
    output_dir = Path(str(source["output_dir"]))
    return output_dir if output_dir.is_absolute() else ROOT / output_dir


def write_empty_source_output(source: dict[str, object], public_base_url: str) -> None:
    output_dir = source_output_dir(source)
    output_dir.mkdir(parents=True, exist_ok=True)
    min_score = int(source.get("min_score", DEFAULT_MIN_SCORE))
    title = f"{source['name']} 今日高分 RSS"
    write_rss([], output_dir / "high_score.xml", title, min_score, True, public_base_url)
    write_markdown([], output_dir / "top_articles.md", min_score, title=title)
    write_markdown([], output_dir / "all_articles.md", 0, title=f"{source['name']} 今日全量评分")
    write_opml([], output_dir / "reeder_high_score_sources.opml")
    write_json([], output_dir / "scored_items.json")


def update_source(source: dict[str, object], public_base_url: str) -> str:
    name = str(source["name"])
    url = str(source["url"])
    feed_file = download_feed(name, url)
    command = [
        sys.executable,
        "rss_reeder_ranker.py",
        str(feed_file),
        "--source-url",
        url,
        "--output-dir",
        str(source["output_dir"]),
        "--min-score",
        str(source.get("min_score", DEFAULT_MIN_SCORE)),
        "--limit",
        str(source.get("limit", 30)),
        "--max-per-feed",
        str(source.get("max_per_feed", 20)),
        "--batch-size",
        str(source.get("batch_size", 3)),
        "--page-char-limit",
        str(source.get("page_char_limit", 5000)),
        "--page-timeout",
        str(source.get("page_timeout", 5)),
        "--page-retries",
        str(source.get("page_retries", 1)),
        "--fetch-pages",
        "--today",
        "--today-timezone",
        SHANGHAI_TIMEZONE,
    ]
    if public_base_url:
        command.extend(
            [
                "--channel-link",
                f"{public_base_url.rstrip('/')}/sources/{name}/high_score.xml",
            ]
        )
    for path in source.get("include_url_paths", []):
        command.extend(["--include-url-path", str(path)])
    for prefix in source.get("include_title_prefixes", []):
        command.extend(["--include-title-prefix", str(prefix)])
    result = run(command)
    if result.returncode != 0:
        if "没有抓到可评分的文章" in result.stdout or "过滤后没有可评分的文章" in result.stdout:
            write_empty_source_output(source, public_base_url)
            return f"## {name}\n当天暂无新文章，已发布空的今日源。\n{result.stdout.strip()}\n"
        raise RuntimeError(f"更新失败：{name}\n{result.stdout}")
    return f"## {name}\n{result.stdout.strip()}\n"


def load_scored_items(path: Path) -> list[ScoredItem]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    scored: list[ScoredItem] = []
    for row in rows:
        item_data = row.get("item", {})
        scored.append(
            ScoredItem(
                item=FeedItem(**item_data),
                score=int(row.get("score", 0)),
                reason=str(row.get("reason", "")),
                optimized_title=str(row.get("optimized_title", item_data.get("title", ""))),
                tags=[str(tag) for tag in row.get("tags", [])][:3],
                priority=str(row.get("priority", "low")),
                ai_summary=str(row.get("ai_summary", "")),
                confidence=str(row.get("confidence", "medium")),
            )
        )
    return scored


def canonical_item_key(item: FeedItem) -> str:
    if not item.link:
        return item.id
    parsed = urllib.parse.urlparse(item.link)
    clean_query = urllib.parse.urlencode(
        [(key, value) for key, value in urllib.parse.parse_qsl(parsed.query) if not key.lower().startswith("utm_")]
    )
    return urllib.parse.urlunparse(parsed._replace(query=clean_query, fragment="")).rstrip("/")


def build_daily_digest(
    sources: list[dict[str, object]],
    public_base_url: str,
    limit: int = DEFAULT_DIGEST_LIMIT,
) -> list[ScoredItem]:
    by_article: dict[str, ScoredItem] = {}
    for source in sources:
        scored_path = source_output_dir(source) / "scored_items.json"
        if not scored_path.exists():
            continue
        min_score = int(source.get("min_score", DEFAULT_MIN_SCORE))
        for scored_item in load_scored_items(scored_path):
            if scored_item.score < min_score:
                continue
            key = canonical_item_key(scored_item.item)
            current = by_article.get(key)
            if current is None or scored_item.score > current.score:
                by_article[key] = scored_item

    selected = sorted(
        by_article.values(),
        key=lambda row: (row.score, row.item.published, row.item.id),
        reverse=True,
    )[:limit]
    DAILY_DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    channel_link = f"{public_base_url.rstrip('/')}/high_score.xml" if public_base_url else ""
    write_rss(
        selected,
        DAILY_DIGEST_DIR / "high_score.xml",
        "今日高分信息源",
        DEFAULT_MIN_SCORE,
        True,
        channel_link=channel_link,
    )
    write_markdown(selected, DAILY_DIGEST_DIR / "top_articles.md", DEFAULT_MIN_SCORE, title="今日高分信息源")
    write_json(selected, DAILY_DIGEST_DIR / "scored_items.json")
    write_opml(selected, DAILY_DIGEST_DIR / "reeder_high_score_sources.opml")
    return selected


def write_index(publish_root: Path, sources: list[dict[str, object]], article_count: int) -> None:
    updated = dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    source_links = "\n".join(
        f'<li><a href="sources/{html.escape(str(source["name"]))}/high_score.xml">'
        f'{html.escape(str(source["name"]))}</a></li>'
        for source in sources
    )
    page = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>今日高分信息源</title>
  <style>
    body {{ max-width: 720px; margin: 48px auto; padding: 0 20px; font: 16px/1.65 system-ui, sans-serif; color: #202124; }}
    h1 {{ font-size: 28px; }}
    a {{ color: #0b57d0; }}
    .meta {{ color: #5f6368; }}
  </style>
</head>
<body>
  <h1>今日高分信息源</h1>
  <p><a href="high_score.xml">订阅统一高分 RSS</a></p>
  <p class="meta">本次精选 {article_count} 篇，更新时间：{html.escape(updated)}</p>
  <h2>独立来源</h2>
  <ul>{source_links}</ul>
</body>
</html>
"""
    (publish_root / "index.html").write_text(page, encoding="utf-8")
    (publish_root / ".nojekyll").write_text("", encoding="utf-8")


def remove_public_page_text(path: Path) -> None:
    if not path.exists():
        return
    rows = json.loads(path.read_text(encoding="utf-8"))
    for row in rows:
        item = row.get("item")
        if isinstance(item, dict):
            item.pop("page_text", None)
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def publish_reeder_outputs(
    publish_root: Path,
    sources: list[dict[str, object]],
    article_count: int,
) -> str:
    resolved_root = publish_root.resolve()
    if resolved_root == ROOT.resolve() or resolved_root == Path("/"):
        raise RuntimeError(f"拒绝覆盖不安全的发布目录：{resolved_root}")
    if publish_root.exists():
        shutil.rmtree(publish_root)
    publish_root.mkdir(parents=True, exist_ok=True)

    shutil.copy2(DAILY_DIGEST_DIR / "high_score.xml", publish_root / "high_score.xml")
    shutil.copytree(DAILY_DIGEST_DIR, publish_root / "daily_digest")
    remove_public_page_text(publish_root / "daily_digest" / "scored_items.json")
    for source in sources:
        public_source_dir = publish_root / "sources" / str(source["name"])
        shutil.copytree(source_output_dir(source), public_source_dir)
        remove_public_page_text(public_source_dir / "scored_items.json")
    write_index(publish_root, sources, article_count)
    return f"## publish\nReeder 发布目录已更新：{publish_root.resolve()}\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="更新每日 RSS、生成统一精选源并准备静态发布目录。")
    parser.add_argument(
        "--publish-dir",
        default=os.getenv("RSS_PUBLISH_DIR", str(DEFAULT_PUBLISH_ROOT)),
        help="静态发布目录；云端使用 site，本地默认沿用原 Reeder 目录",
    )
    parser.add_argument("--public-base-url", default=os.getenv("PUBLIC_BASE_URL", ""), help="线上公开根地址")
    parser.add_argument("--digest-limit", type=int, default=DEFAULT_DIGEST_LIMIT, help="统一精选源最多文章数")
    parser.add_argument("--require-api-key", action="store_true", help="缺少 DeepSeek Key 时直接失败")
    return parser


def prepare_generated_outputs(sources: list[dict[str, object]]) -> None:
    generated_dirs = [source_output_dir(source) for source in sources] + [DAILY_DIGEST_DIR]
    output_root = OUTPUT_DIR.resolve()
    for generated_dir in generated_dirs:
        resolved = generated_dir.resolve()
        if output_root not in resolved.parents:
            raise RuntimeError(f"拒绝清理 output 目录之外的路径：{resolved}")
        if generated_dir.exists():
            shutil.rmtree(generated_dir)


def main() -> int:
    args = build_parser().parse_args()
    load_dotenv(ROOT / ".env")
    if args.require_api_key and not os.getenv("DEEPSEEK_API_KEY"):
        print("缺少 DEEPSEEK_API_KEY，已停止，避免使用本地规则覆盖线上结果。", file=sys.stderr)
        return 2

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    sources = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    prepare_generated_outputs(sources)
    started = dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    sections = [f"# Daily RSS update\n\nStarted: {started}\n"]
    failures: list[str] = []
    successful_sources = 0
    for source in sources:
        try:
            sections.append(update_source(source, args.public_base_url))
            successful_sources += 1
        except Exception as exc:  # noqa: BLE001 - update other feeds even if one fails.
            failures.append(str(exc))
            write_empty_source_output(source, args.public_base_url)
            sections.append(f"## {source.get('name', 'unknown')}\nFAILED\n{exc}\n")

    try:
        selected = build_daily_digest(sources, args.public_base_url, max(1, args.digest_limit))
        sections.append(f"## daily_digest\n统一精选 {len(selected)} 篇。\n")
        sections.append(publish_reeder_outputs(Path(args.publish_dir), sources, len(selected)))
    except Exception as exc:  # noqa: BLE001 - make publishing failures visible.
        failures.append(str(exc))
        sections.append(f"## publish\nFAILED\n{exc}\n")

    finished = dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    sections.append(f"Finished: {finished}\n")
    log_text = "\n".join(sections)
    log_path = LOG_DIR / "daily_update_latest.md"
    log_path.write_text(log_text, encoding="utf-8")
    dated_log = LOG_DIR / f"daily_update_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    dated_log.write_text(log_text, encoding="utf-8")
    print(log_path)
    return 1 if failures and successful_sources == 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
