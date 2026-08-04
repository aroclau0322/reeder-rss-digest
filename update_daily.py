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
    parse_datetime,
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
DEFAULT_DIGEST_LIMIT = 100
HISTORY_DAYS = 3
INITIAL_LOOKBACK_HOURS = 48
MAX_SOURCE_HISTORY = 500


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


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_state_time(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def read_json_file(path: Path) -> object | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def load_public_json(publish_root: Path, public_base_url: str, relative_path: str) -> object | None:
    local_payload = read_json_file(publish_root / relative_path)
    if local_payload is not None:
        return local_payload
    if not public_base_url:
        return None
    result = run(
        [
            "curl",
            "-fsSL",
            "--retry",
            "1",
            "--connect-timeout",
            "8",
            "--max-time",
            "20",
            f"{public_base_url.rstrip('/')}/{relative_path}",
        ]
    )
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def write_empty_source_output(source: dict[str, object], public_base_url: str) -> None:
    output_dir = source_output_dir(source)
    output_dir.mkdir(parents=True, exist_ok=True)
    min_score = int(source.get("min_score", DEFAULT_MIN_SCORE))
    title = f"{source['name']} 近 {HISTORY_DAYS} 日高分 RSS"
    write_rss([], output_dir / "high_score.xml", title, min_score, True, public_base_url)
    write_markdown([], output_dir / "top_articles.md", min_score, title=title)
    write_markdown([], output_dir / "all_articles.md", 0, title=f"{source['name']} 近 {HISTORY_DAYS} 日全量评分")
    write_opml([], output_dir / "reeder_high_score_sources.opml")
    write_json([], output_dir / "scored_items.json")


def update_source(
    source: dict[str, object],
    public_base_url: str,
    since: dt.datetime,
) -> tuple[str, dict[str, object]]:
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
        "--page-workers",
        str(source.get("page_workers", 4)),
        "--fetch-pages",
        "--since",
        since.astimezone(dt.timezone.utc).isoformat(),
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
    stats_payload = read_json_file(source_output_dir(source) / "run_stats.json")
    stats = stats_payload if isinstance(stats_payload, dict) else {}
    if result.returncode != 0:
        if "没有抓到可评分的文章" in result.stdout or "过滤后没有可评分的文章" in result.stdout:
            write_empty_source_output(source, public_base_url)
            return f"## {name}\n本轮暂无新文章，继续保留历史结果。\n{result.stdout.strip()}\n", stats
        raise RuntimeError(f"更新失败：{name}\n{result.stdout}")
    return f"## {name}\n{result.stdout.strip()}\n", stats


def scored_items_from_rows(rows: object) -> list[ScoredItem]:
    if not isinstance(rows, list):
        return []
    scored: list[ScoredItem] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item_data = row.get("item", {})
        if not isinstance(item_data, dict):
            continue
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


def load_scored_items(path: Path) -> list[ScoredItem]:
    return scored_items_from_rows(read_json_file(path))


def canonical_item_key(item: FeedItem) -> str:
    if not item.link:
        return item.id
    parsed = urllib.parse.urlparse(item.link)
    clean_query = urllib.parse.urlencode(
        [(key, value) for key, value in urllib.parse.parse_qsl(parsed.query) if not key.lower().startswith("utm_")]
    )
    return urllib.parse.urlunparse(parsed._replace(query=clean_query, fragment="")).rstrip("/")


def scored_sort_key(scored_item: ScoredItem) -> tuple[float, int, str]:
    published = parse_datetime(scored_item.item.published)
    timestamp = published.timestamp() if published else 0.0
    return timestamp, scored_item.score, scored_item.item.id


def merge_scored_history(
    previous: list[ScoredItem],
    current: list[ScoredItem],
    cutoff: dt.datetime,
    limit: int = MAX_SOURCE_HISTORY,
) -> list[ScoredItem]:
    by_article: dict[str, ScoredItem] = {}
    for scored_item in [*previous, *current]:
        published = parse_datetime(scored_item.item.published)
        if published and published < cutoff:
            continue
        by_article[canonical_item_key(scored_item.item)] = scored_item
    return sorted(by_article.values(), key=scored_sort_key, reverse=True)[:limit]


def write_source_history(
    source: dict[str, object],
    scored: list[ScoredItem],
    public_base_url: str,
) -> tuple[int, int]:
    output_dir = source_output_dir(source)
    output_dir.mkdir(parents=True, exist_ok=True)
    min_score = int(source.get("min_score", DEFAULT_MIN_SCORE))
    history_limit = int(source.get("history_limit", 100))
    selected = sorted(
        [row for row in scored if row.score >= min_score],
        key=scored_sort_key,
        reverse=True,
    )[:history_limit]
    name = str(source["name"])
    title = f"{name} 近 {HISTORY_DAYS} 日高分 RSS"
    channel_link = (
        f"{public_base_url.rstrip('/')}/sources/{name}/high_score.xml"
        if public_base_url
        else "http://127.0.0.1/rss-reeder-ranker"
    )
    write_rss(selected, output_dir / "high_score.xml", title, min_score, True, channel_link)
    write_markdown(selected, output_dir / "top_articles.md", min_score, title=title)
    write_markdown(scored, output_dir / "all_articles.md", 0, title=f"{name} 近 {HISTORY_DAYS} 日全量评分")
    write_opml(selected, output_dir / "reeder_high_score_sources.opml")
    write_json(scored, output_dir / "scored_items.json")
    return len(scored), len(selected)


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
        f"近 {HISTORY_DAYS} 日高分信息源",
        DEFAULT_MIN_SCORE,
        True,
        channel_link=channel_link,
    )
    write_markdown(
        selected,
        DAILY_DIGEST_DIR / "top_articles.md",
        DEFAULT_MIN_SCORE,
        title=f"近 {HISTORY_DAYS} 日高分信息源",
    )
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
  <title>近 {HISTORY_DAYS} 日高分信息源</title>
  <style>
    body {{ max-width: 720px; margin: 48px auto; padding: 0 20px; font: 16px/1.65 system-ui, sans-serif; color: #202124; }}
    h1 {{ font-size: 28px; }}
    a {{ color: #0b57d0; }}
    .meta {{ color: #5f6368; }}
  </style>
</head>
<body>
  <h1>近 {HISTORY_DAYS} 日高分信息源</h1>
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
    state: dict[str, object],
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
    (publish_root / "state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
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
    publish_root = Path(args.publish_dir)
    previous_state_payload = load_public_json(publish_root, args.public_base_url, "state.json")
    previous_state = previous_state_payload if isinstance(previous_state_payload, dict) else {}
    previous_source_state_payload = previous_state.get("sources", {})
    previous_source_state = previous_source_state_payload if isinstance(previous_source_state_payload, dict) else {}
    previous_items: dict[str, list[ScoredItem]] = {}
    for source in sources:
        name = str(source["name"])
        payload = load_public_json(
            publish_root,
            args.public_base_url,
            f"sources/{name}/scored_items.json",
        )
        previous_items[name] = scored_items_from_rows(payload)

    prepare_generated_outputs(sources)
    run_started = utc_now()
    history_cutoff = run_started - dt.timedelta(days=HISTORY_DAYS)
    started = dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    sections = [f"# Daily RSS update\n\nStarted: {started}\n"]
    failures: list[str] = []
    successful_sources = 0
    state_sources: dict[str, object] = {}
    for source in sources:
        name = str(source["name"])
        prior_state_payload = previous_source_state.get(name, {})
        prior_state = prior_state_payload if isinstance(prior_state_payload, dict) else {}
        since = parse_state_time(prior_state.get("last_successful_at"))
        if since is None:
            since = run_started - dt.timedelta(hours=INITIAL_LOOKBACK_HOURS)
        try:
            source_log, stats = update_source(source, args.public_base_url, since)
            current_items = load_scored_items(source_output_dir(source) / "scored_items.json")
            merged = merge_scored_history(previous_items[name], current_items, history_cutoff)
            retained_count, retained_high_score = write_source_history(source, merged, args.public_base_url)
            counts = {
                **stats,
                "new_scored": len(current_items),
                "retained_history": retained_count,
                "retained_high_score": retained_high_score,
            }
            state_sources[name] = {
                "last_successful_at": run_started.isoformat(),
                "since": since.isoformat(),
                "status": "success",
                "counts": counts,
            }
            sections.append(source_log)
            sections.append(
                "统计："
                f"原始 {counts.get('raw_feed_items', 0)}，"
                f"时间过滤后 {counts.get('after_time_filter', 0)}，"
                f"源上限后 {counts.get('after_source_cap', 0)}，"
                f"频道过滤后 {counts.get('after_channel_filter', 0)}，"
                f"本轮评分 {len(current_items)}，"
                f"三日保留 {retained_count}，其中高分 {retained_high_score}。\n"
            )
            successful_sources += 1
        except Exception as exc:  # noqa: BLE001 - update other feeds even if one fails.
            failures.append(str(exc))
            retained = merge_scored_history(previous_items[name], [], history_cutoff)
            retained_count, retained_high_score = write_source_history(source, retained, args.public_base_url)
            state_sources[name] = {
                "last_successful_at": prior_state.get("last_successful_at", ""),
                "since": since.isoformat(),
                "status": "failed",
                "error": str(exc),
                "counts": {
                    "retained_history": retained_count,
                    "retained_high_score": retained_high_score,
                },
            }
            sections.append(f"## {name}\nFAILED，已保留上次结果\n{exc}\n")

    state: dict[str, object] = {
        "version": 1,
        "updated_at": utc_now().isoformat(),
        "history_days": HISTORY_DAYS,
        "initial_lookback_hours": INITIAL_LOOKBACK_HOURS,
        "sources": state_sources,
    }

    try:
        selected = build_daily_digest(sources, args.public_base_url, max(1, args.digest_limit))
        sections.append(f"## daily_digest\n近 {HISTORY_DAYS} 日统一精选 {len(selected)} 篇。\n")
        sections.append(publish_reeder_outputs(publish_root, sources, len(selected), state))
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
