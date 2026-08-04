#!/usr/bin/env python3
"""Rank RSS/Atom items with DeepSeek and export Reeder-friendly results."""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import email.utils
import hashlib
import html
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"
USER_AGENT = "rss-reeder-ranker/1.0 (+https://api.deepseek.com)"


try:
    import certifi
except ImportError:  # pragma: no cover - optional local certificate helper.
    certifi = None


@dataclass
class FeedSource:
    title: str
    url: str


@dataclass
class FeedItem:
    id: str
    feed_title: str
    feed_url: str
    title: str
    link: str
    summary: str
    published: str
    page_text: str = ""
    page_status: str = "rss_only"


@dataclass
class ScoredItem:
    item: FeedItem
    score: int
    reason: str
    optimized_title: str
    tags: list[str]
    priority: str
    ai_summary: str = ""
    confidence: str = "medium"


def https_context(insecure_tls: bool = False) -> ssl.SSLContext | None:
    if insecure_tls:
        return ssl._create_unverified_context()
    if certifi is not None:
        return ssl.create_default_context(cafile=certifi.where())
    return None


def fetch_text(url: str, timeout: int = 20, insecure_tls: bool = False) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    context = https_context(insecure_tls)
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        raw = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace")


def fetch_page_html(
    url: str,
    timeout: int,
    insecure_tls: bool,
    max_bytes: int,
) -> tuple[str, bool, str]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
        },
    )
    response = urllib.request.urlopen(request, timeout=timeout, context=https_context(insecure_tls))
    chunks: list[bytes] = []
    total = 0
    complete = True
    read_note = ""
    charset = response.headers.get_content_charset() or "utf-8"
    try:
        while total < max_bytes:
            try:
                chunk = response.read(min(65536, max_bytes - total))
            except (TimeoutError, socket.timeout, urllib.error.URLError) as exc:
                if not chunks:
                    raise
                complete = False
                read_note = f"页面连接提前结束：{exc}"
                break
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total >= max_bytes:
            complete = False
            read_note = f"页面超过 {max_bytes} bytes，已停止继续下载"
    finally:
        response.close()
    return b"".join(chunks).decode(charset, errors="replace"), complete, read_note


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def read_input(source: str, insecure_tls: bool = False) -> str:
    if source == "-":
        return sys.stdin.read()
    parsed = urllib.parse.urlparse(source)
    if parsed.scheme in {"http", "https"}:
        return fetch_text(source, insecure_tls=insecure_tls)
    return Path(source).read_text(encoding="utf-8")


def strip_html(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value or "")
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def short_text(value: str, limit: int) -> str:
    value = strip_html(value)
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def page_to_text(value: str, limit: int) -> str:
    text = strip_html(value)
    noisy_patterns = [
        r"(?i)window\.__.*",
        r"(?i)copyright.*",
        r"(?i)登录.*?注册",
    ]
    for pattern in noisy_patterns:
        text = re.sub(pattern, " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return short_text(text, limit)


ARTICLE_BLOCK_TAGS = {"p", "div", "section", "article", "main", "h1", "h2", "h3", "h4", "li", "blockquote", "br"}
ARTICLE_IGNORED_TAGS = {"script", "style", "noscript", "svg", "nav", "header", "footer", "aside", "form", "button"}
HTML_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
ARTICLE_CLASS_PRIORITIES = {
    "article__content": 120,
    "article-content": 115,
    "article_content": 115,
    "article-body": 115,
    "article_body": 115,
    "post-content": 110,
    "entry-content": 110,
    "detail-content": 105,
    "rich_media_content": 105,
}
PAYWALL_PATTERNS = (
    "登录后阅读全文",
    "开通会员阅读全文",
    "会员专享",
    "本文为付费内容",
    "付费后阅读全文",
    "剩余内容仅会员可见",
)


def normalize_article_text(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"[\t\r\f\v ]+", " ", value)
    value = re.sub(r" *\n *", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


class ArticleHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.ignored_depth = 0
        self.active: list[dict[str, Any]] = []
        self.candidates: list[dict[str, Any]] = []
        self.json_script_depth = 0
        self.json_buffer: list[str] = []
        self.json_scripts: list[str] = []

    def candidate_priority(self, tag: str, attrs: dict[str, str]) -> int:
        classes = attrs.get("class", "").split()
        for class_name in classes:
            if class_name in ARTICLE_CLASS_PRIORITIES:
                return ARTICLE_CLASS_PRIORITIES[class_name]
        joined = " ".join(classes).lower()
        if re.search(r"(?:article|post|entry|detail).*(?:content|body)", joined):
            return 95
        if tag == "article":
            return 90
        if tag == "main":
            return 70
        return 0

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        if tag in HTML_VOID_TAGS:
            if tag == "br" and not self.ignored_depth:
                for candidate in self.active:
                    candidate["parts"].append("\n")
            return
        self.depth += 1
        attrs = {key: value or "" for key, value in attrs_list}
        if tag == "script" and "json" in attrs.get("type", "").lower():
            self.json_script_depth = self.depth
            self.json_buffer = []
        if tag in ARTICLE_IGNORED_TAGS:
            self.ignored_depth += 1
        priority = self.candidate_priority(tag, attrs)
        if priority:
            self.active.append({"depth": self.depth, "priority": priority, "parts": [], "complete": False})
        if not self.ignored_depth and tag in ARTICLE_BLOCK_TAGS:
            for candidate in self.active:
                candidate["parts"].append("\n")

    def handle_startendtag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        if tag == "br" and not self.ignored_depth:
            for candidate in self.active:
                candidate["parts"].append("\n")

    def handle_data(self, data: str) -> None:
        if self.json_script_depth:
            self.json_buffer.append(data)
        if self.ignored_depth:
            return
        for candidate in self.active:
            candidate["parts"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self.ignored_depth and tag in ARTICLE_BLOCK_TAGS:
            for candidate in self.active:
                candidate["parts"].append("\n")
        ending = [candidate for candidate in self.active if candidate["depth"] == self.depth]
        for candidate in ending:
            candidate["complete"] = True
            self.candidates.append(candidate)
            self.active.remove(candidate)
        if tag in ARTICLE_IGNORED_TAGS and self.ignored_depth:
            self.ignored_depth -= 1
        if tag == "script" and self.json_script_depth == self.depth:
            self.json_scripts.append("".join(self.json_buffer))
            self.json_script_depth = 0
            self.json_buffer = []
        self.depth = max(0, self.depth - 1)

    def finish(self) -> list[dict[str, Any]]:
        self.candidates.extend(self.active)
        self.active = []
        return self.candidates


def json_article_bodies(value: Any) -> list[str]:
    bodies: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() == "articlebody" and isinstance(child, str):
                bodies.append(child)
            else:
                bodies.extend(json_article_bodies(child))
    elif isinstance(value, list):
        for child in value:
            bodies.extend(json_article_bodies(child))
    return bodies


def extract_article_text(value: str, limit: int, download_complete: bool) -> tuple[str, str]:
    parser = ArticleHTMLParser()
    parser.feed(value)
    candidates = parser.finish()
    for raw_json in parser.json_scripts:
        try:
            for body in json_article_bodies(json.loads(raw_json)):
                candidates.append(
                    {
                        "priority": 108 if len(strip_html(body)) >= 1200 else 60,
                        "parts": [strip_html(body)],
                        "complete": True,
                    }
                )
        except (json.JSONDecodeError, TypeError):
            continue

    normalized: list[tuple[int, int, str, bool]] = []
    for candidate in candidates:
        text = normalize_article_text("".join(candidate["parts"]))
        if len(text) >= 200:
            normalized.append((int(candidate["priority"]), len(text), text, bool(candidate["complete"])))

    if normalized:
        _, original_length, text, container_complete = max(normalized, key=lambda row: (row[0], row[1]))
    else:
        text = page_to_text(value, max(limit, 500))
        original_length = len(text)
        container_complete = False

    paywalled = any(pattern in value for pattern in PAYWALL_PATTERNS)
    truncated = original_length > limit
    if truncated:
        text = text[:limit].rstrip() + "…"
    if paywalled:
        status = "paywalled"
    elif truncated or not container_complete:
        status = "partial"
    elif download_complete or container_complete:
        status = "full"
    else:
        status = "partial"
    return text, status


def namespace_free(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def child_text(node: ET.Element, names: set[str]) -> str:
    for child in list(node):
        if namespace_free(child.tag) in names and child.text:
            return child.text.strip()
    return ""


def find_atom_link(node: ET.Element) -> str:
    fallback = ""
    for child in list(node):
        if namespace_free(child.tag) != "link":
            continue
        href = child.attrib.get("href", "").strip()
        rel = child.attrib.get("rel", "alternate")
        if href and rel == "alternate":
            return href
        if href and not fallback:
            fallback = href
        if child.text and not fallback:
            fallback = child.text.strip()
    return fallback


def parse_datetime(value: str) -> dt.datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            parsed = dt.datetime.strptime(value, fmt)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
    return None


def item_id(feed_url: str, title: str, link: str) -> str:
    seed = f"{feed_url}\n{title}\n{link}".encode("utf-8", errors="ignore")
    return hashlib.sha1(seed).hexdigest()[:12]


def is_opml(text: str) -> bool:
    return "<opml" in text[:500].lower()


def parse_sources(source: str, text: str) -> list[FeedSource]:
    if is_opml(text):
        root = ET.fromstring(text)
        sources: list[FeedSource] = []
        for outline in root.iter():
            if namespace_free(outline.tag) != "outline":
                continue
            url = outline.attrib.get("xmlUrl") or outline.attrib.get("xmlurl")
            if not url:
                continue
            title = outline.attrib.get("title") or outline.attrib.get("text") or url
            sources.append(FeedSource(title=title.strip(), url=url.strip()))
        return dedupe_sources(sources)

    lines = [line.strip() for line in text.splitlines()]
    urls = [line for line in lines if line and not line.startswith("#")]
    if len(urls) > 1 and all(urllib.parse.urlparse(line).scheme in {"http", "https"} for line in urls):
        return dedupe_sources([FeedSource(title=line, url=line) for line in urls])

    parsed = urllib.parse.urlparse(source)
    if parsed.scheme in {"http", "https"}:
        return [FeedSource(title=source, url=source)]
    try:
        root = ET.fromstring(text)
        if namespace_free(root.tag) in {"rss", "feed"}:
            return [FeedSource(title=source, url=source)]
    except ET.ParseError:
        pass
    raise ValueError("输入需要是 RSS/Atom URL、OPML 文件，或每行一个订阅源 URL 的文本文件。")


def override_single_source_url(sources: list[FeedSource], source_url: str) -> list[FeedSource]:
    if not source_url or len(sources) != 1:
        return sources
    return [FeedSource(title=sources[0].title, url=source_url)]


def dedupe_sources(sources: list[FeedSource]) -> list[FeedSource]:
    seen: set[str] = set()
    unique: list[FeedSource] = []
    for source in sources:
        if source.url in seen:
            continue
        seen.add(source.url)
        unique.append(source)
    return unique


def parse_feed(
    source: FeedSource,
    text: str,
    max_per_feed: int,
    since: dt.datetime | None,
    stats: dict[str, int] | None = None,
) -> list[FeedItem]:
    root = ET.fromstring(text)
    root_name = namespace_free(root.tag)
    channel = root.find("channel") if root_name == "rss" else None
    feed_title = source.title
    items: list[ET.Element]

    if channel is not None:
        feed_title = child_text(channel, {"title"}) or feed_title
        items = [child for child in list(channel) if namespace_free(child.tag) == "item"]
    elif root_name == "feed":
        feed_title = child_text(root, {"title"}) or feed_title
        items = [child for child in list(root) if namespace_free(child.tag) == "entry"]
    else:
        items = [child for child in root.iter() if namespace_free(child.tag) in {"item", "entry"}]

    if stats is not None:
        stats["raw_feed_items"] = stats.get("raw_feed_items", 0) + len(items)

    parsed_items: list[FeedItem] = []
    for node in items:
        title = strip_html(child_text(node, {"title"})) or "(untitled)"
        link = child_text(node, {"link"}) or find_atom_link(node)
        summary = (
            child_text(node, {"description", "summary", "content", "encoded"})
            or child_text(node, {"subtitle"})
        )
        published = child_text(node, {"pubdate", "published", "updated", "date"})
        published_dt = parse_datetime(published)
        if since and published_dt and published_dt < since:
            continue
        parsed_items.append(
            FeedItem(
                id=item_id(source.url, title, link),
                feed_title=feed_title,
                feed_url=source.url,
                title=title,
                link=link,
                summary=short_text(summary, 1200),
                published=published,
            )
        )
    if stats is not None:
        stats["after_time_filter"] = stats.get("after_time_filter", 0) + len(parsed_items)
    capped_items = parsed_items[:max_per_feed]
    if stats is not None:
        stats["after_source_cap"] = stats.get("after_source_cap", 0) + len(capped_items)
    return capped_items


def filter_items(
    items: list[FeedItem],
    include_url_paths: list[str],
    include_title_prefixes: list[str],
) -> list[FeedItem]:
    if not include_url_paths and not include_title_prefixes:
        return items
    filtered: list[FeedItem] = []
    for item in items:
        link_path = urllib.parse.urlparse(item.link).path
        url_ok = not include_url_paths or any(path in link_path for path in include_url_paths)
        title_ok = not include_title_prefixes or any(item.title.strip().startswith(prefix) for prefix in include_title_prefixes)
        if url_ok and title_ok:
            filtered.append(item)
    return filtered


def heuristic_score(item: FeedItem, rubric: str) -> ScoredItem:
    text = f"{item.title} {item.summary}".lower()
    positive = [
        "analysis",
        "deep dive",
        "research",
        "report",
        "case study",
        "数据",
        "复盘",
        "分析",
        "研究",
        "趋势",
        "方法",
        "案例",
        "深度",
    ]
    negative = ["sponsored", "deal", "sale", "coupon", "广告", "促销", "招聘"]
    score = 50 + sum(8 for word in positive if word in text) - sum(10 for word in negative if word in text)
    if len(strip_html(item.summary)) > 350:
        score += 8
    if any(word in text for word in rubric.lower().split()[:20]):
        score += 4
    score = max(0, min(100, score))
    return ScoredItem(
        item=item,
        score=score,
        reason="本地规则评分，用于没有 DeepSeek API Key 时预览流程。",
        optimized_title=item.title,
        tags=[],
        priority="high" if score >= 85 else "medium" if score >= 70 else "low",
        ai_summary=short_text(item.page_text or item.summary, 220),
        confidence=confidence_for_page_status(item.page_status),
    )


def confidence_for_page_status(page_status: str) -> str:
    if page_status == "full":
        return "high"
    if page_status == "partial":
        return "medium"
    return "low"


def deepseek_chat(
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int = 4096,
    timeout: int = 60,
) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "stream": False,
    }
    request = urllib.request.Request(
        f"{DEEPSEEK_BASE_URL}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout, context=https_context()) as response:
        return json.loads(response.read().decode("utf-8"))


def parse_json_object(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            raise
        return json.loads(match.group(0))


def score_batch(items: list[FeedItem], rubric: str, api_key: str, model: str) -> list[ScoredItem]:
    sample = [
        {
            "id": item.id,
            "feed": item.feed_title,
            "title": item.title,
            "summary": short_text(item.summary, 800),
            "page_excerpt": short_text(item.page_text, 15000) if item.page_text else "",
            "page_status": item.page_status,
            "published": item.published,
            "link": item.link,
        }
        for item in items
    ]
    system = (
        "你是一个为 Reeder/RSS 用户筛选信息源的中文编辑。"
        "请严格输出 JSON，不要输出 Markdown。"
    )
    user = {
        "task": "给 RSS 条目打分、优化标题，并为每个已浏览页面写一段中文总结，输出 json。",
        "rubric": rubric,
        "scoring": {
            "score": "0-100，越值得读越高；90+ 是必读，75-89 是值得读，60-74 可选，60 以下忽略。",
            "consider": [
                "信息密度：是否提供事实、细节、步骤、数据或清晰观察",
                "原创性：是否有作者自己的经验、判断、方法或案例，而不是转述",
                "长期价值：一周后、一个月后是否仍值得保存",
                "偏好匹配：是否贴合用户在 rubric 中写下的主题和口味",
                "行动价值：是否能帮助做决策、改流程、选工具、形成素材",
                "扣分项：营销软文、标题党、低质量转载、快讯噪音、信息不完整",
            ],
        },
        "summary_rules": [
            "ai_summary 用中文写 80-160 字。",
            "总结要说明文章主要讲什么、最值得看的点是什么。",
            "不要复述评分标准，不要写空泛赞美。",
            "如果 page_excerpt 为空，就基于 RSS summary 总结，并在措辞上保守。",
            "page_status=full 表示正文完整；partial 表示正文被截断；paywalled 表示只有付费预览；rss_only 表示没有抓到页面。",
            "正文不完整时不要假装读过全文，并降低 confidence。",
        ],
        "output_schema": {
            "items": [
                {
                    "id": "和输入一致",
                    "score": 88,
                    "reason": "一句中文说明",
                    "optimized_title": "更适合稍后阅读列表的中文标题",
                    "tags": ["最多3个中文标签"],
                    "priority": "high|medium|low",
                    "ai_summary": "80-160字中文页面总结",
                    "confidence": "high|medium|low，表示评分依据的完整度",
                }
            ]
        },
        "items": sample,
    }
    response = deepseek_chat(
        api_key=api_key,
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
        ],
    )
    content = response["choices"][0]["message"]["content"]
    result = parse_json_object(content)
    if isinstance(result, list):
        result_items = result
    else:
        result_items = result.get("items", [])
    by_id = {entry.get("id"): entry for entry in result_items if isinstance(entry, dict)}

    scored: list[ScoredItem] = []
    for item in items:
        entry = by_id.get(item.id, {})
        try:
            score = int(entry.get("score", 0))
        except (TypeError, ValueError):
            score = 0
        scored.append(
            ScoredItem(
                item=item,
                score=max(0, min(100, score)),
                reason=str(entry.get("reason") or "DeepSeek 未返回理由。"),
                optimized_title=str(entry.get("optimized_title") or item.title),
                tags=[str(tag) for tag in entry.get("tags", [])][:3] if isinstance(entry.get("tags"), list) else [],
                priority=str(entry.get("priority") or ("high" if score >= 85 else "medium" if score >= 70 else "low")),
                ai_summary=str(entry.get("ai_summary") or short_text(item.page_text or item.summary, 220)),
                confidence=str(entry.get("confidence") or confidence_for_page_status(item.page_status)),
            )
        )
    return scored


def score_items(
    items: list[FeedItem],
    rubric: str,
    api_key: str | None,
    model: str,
    batch_size: int,
    sleep_seconds: float,
) -> list[ScoredItem]:
    if not api_key:
        return [heuristic_score(item, rubric) for item in items]

    def score_with_retry(batch: list[FeedItem]) -> list[ScoredItem]:
        last_error: Exception | None = None
        attempts = 2 if len(batch) > 1 else 3
        for attempt in range(attempts):
            try:
                return score_batch(batch, rubric, api_key, model)
            except Exception as exc:  # noqa: BLE001 - malformed model output and transient API errors are retried.
                last_error = exc
                if attempt + 1 < attempts:
                    time.sleep(1.0 * (attempt + 1))

        if len(batch) > 1:
            midpoint = max(1, len(batch) // 2)
            return [
                *score_with_retry(batch[:midpoint]),
                *score_with_retry(batch[midpoint:]),
            ]
        raise RuntimeError(f"DeepSeek 单篇评分连续失败：{batch[0].title}") from last_error

    scored: list[ScoredItem] = []
    for start in range(0, len(items), batch_size):
        batch = items[start : start + batch_size]
        scored.extend(score_with_retry(batch))
        if sleep_seconds and start + batch_size < len(items):
            time.sleep(sleep_seconds)
    return scored


def enrich_items_with_pages(
    items: list[FeedItem],
    page_char_limit: int,
    timeout: int,
    insecure_tls: bool,
    sleep_seconds: float,
    retries: int,
    workers: int,
) -> list[str]:
    def enrich_item(item: FeedItem) -> str:
        parsed = urllib.parse.urlparse(item.link)
        if parsed.scheme not in {"http", "https"}:
            return ""
        last_error = ""
        for attempt in range(max(1, retries + 1)):
            try:
                page_html, download_complete, read_note = fetch_page_html(
                    item.link,
                    timeout=timeout,
                    insecure_tls=insecure_tls,
                    max_bytes=min(2_000_000, max(250_000, page_char_limit * 20)),
                )
                item.page_text, item.page_status = extract_article_text(
                    page_html,
                    page_char_limit,
                    download_complete=download_complete,
                )
                if item.page_text:
                    if read_note and item.page_status != "full":
                        last_error = read_note
                    break
                last_error = "页面返回成功，但未提取到正文"
            except Exception as exc:  # noqa: BLE001 - retry and keep scoring if a page fails.
                last_error = str(exc)
            if attempt < retries:
                time.sleep(min(2.0, 0.5 * (attempt + 1)))
        if not item.page_text:
            item.page_status = "rss_only"
        if sleep_seconds:
            time.sleep(sleep_seconds)
        return f"{item.link}: {last_error}" if last_error else ""

    errors: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = [executor.submit(enrich_item, item) for item in items]
        for future in concurrent.futures.as_completed(futures):
            error = future.result()
            if error:
                errors.append(error)
    return errors


def rss_date(value: str) -> str:
    parsed = parse_datetime(value) or dt.datetime.now(dt.timezone.utc)
    return email.utils.format_datetime(parsed)


def reeder_unique_link(link: str, item_id_value: str) -> str:
    if not link:
        return link
    parsed = urllib.parse.urlparse(link)
    fragment = parsed.fragment
    marker = f"rss-ranker-{item_id_value}"
    if fragment:
        fragment = f"{fragment}-{marker}"
    else:
        fragment = marker
    return urllib.parse.urlunparse(parsed._replace(fragment=fragment))


def xml_text(parent: ET.Element, tag: str, text: str) -> ET.Element:
    child = ET.SubElement(parent, tag)
    child.text = text or ""
    return child


def page_status_label(status: str) -> str:
    return {
        "full": "全文",
        "partial": "正文截断",
        "paywalled": "付费预览",
        "rss_only": "仅 RSS",
    }.get(status, status or "未知")


def write_rss(
    scored: list[ScoredItem],
    path: Path,
    title: str,
    min_score: int,
    unique_for_reeder: bool,
    channel_link: str = "http://127.0.0.1/rss-reeder-ranker",
) -> None:
    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")
    xml_text(channel, "title", title)
    xml_text(channel, "link", channel_link)
    xml_text(channel, "description", f"DeepSeek 筛选出的 {min_score}+ 分 RSS 条目")
    xml_text(channel, "lastBuildDate", email.utils.format_datetime(dt.datetime.now(dt.timezone.utc)))
    for scored_item in scored:
        item = scored_item.item
        link = reeder_unique_link(item.link, item.id) if unique_for_reeder else item.link
        guid = f"rss-reeder-ranker:{item.id}" if unique_for_reeder else (item.link or item.id)
        node = ET.SubElement(channel, "item")
        xml_text(node, "title", scored_item.optimized_title or item.title)
        xml_text(node, "link", link)
        xml_text(node, "guid", guid).set("isPermaLink", "false")
        xml_text(node, "pubDate", rss_date(item.published))
        description = (
            f"<p><strong>Score:</strong> {scored_item.score}/100 · {html.escape(scored_item.priority)}</p>"
            f"<p><strong>Content:</strong> {html.escape(page_status_label(item.page_status))} · "
            f"评分置信度 {html.escape(scored_item.confidence)}</p>"
            f"<p><strong>Why:</strong> {html.escape(scored_item.reason)}</p>"
            f"<p><strong>Summary:</strong> {html.escape(scored_item.ai_summary)}</p>"
            f"<p><strong>Source:</strong> {html.escape(item.feed_title)}</p>"
            f"<p>{html.escape(item.summary)}</p>"
        )
        xml_text(node, "description", description)
        for tag in scored_item.tags:
            xml_text(node, "category", tag)
    ET.ElementTree(rss).write(path, encoding="utf-8", xml_declaration=True)


def write_markdown(scored: list[ScoredItem], path: Path, min_score: int, title: str = "DeepSeek RSS 高分清单") -> None:
    lines = [
        f"# {title}",
        "",
        f"生成时间：{dt.datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"筛选阈值：{min_score}/100",
        "",
    ]
    for index, scored_item in enumerate(scored, start=1):
        item = scored_item.item
        tags = " ".join(f"`{tag}`" for tag in scored_item.tags)
        lines.extend(
            [
                f"## {index}. {scored_item.optimized_title}",
                "",
                f"- 分数：{scored_item.score}/100（{scored_item.priority}）",
                f"- 正文状态：{page_status_label(item.page_status)}；评分置信度：{scored_item.confidence}",
                f"- 来源：{item.feed_title}",
                f"- 链接：{item.link}",
                f"- 理由：{scored_item.reason}",
                f"- 标签：{tags or '无'}",
                f"- 原标题：{item.title}",
                "",
                f"DeepSeek 总结：{scored_item.ai_summary}",
                "",
                short_text(item.summary, 500),
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def write_opml(scored: list[ScoredItem], path: Path) -> None:
    feeds: dict[str, str] = {}
    for scored_item in scored:
        feeds.setdefault(scored_item.item.feed_url, scored_item.item.feed_title)

    opml = ET.Element("opml", version="2.0")
    head = ET.SubElement(opml, "head")
    xml_text(head, "title", "DeepSeek 高分 RSS 来源")
    body = ET.SubElement(opml, "body")
    group = ET.SubElement(body, "outline", text="DeepSeek 高分来源", title="DeepSeek 高分来源")
    for url, title in sorted(feeds.items(), key=lambda pair: pair[1].lower()):
        ET.SubElement(group, "outline", text=title, title=title, type="rss", xmlUrl=url)
    ET.ElementTree(opml).write(path, encoding="utf-8", xml_declaration=True)


def write_json(scored: list[ScoredItem], path: Path) -> None:
    payload = []
    for scored_item in scored:
        row = asdict(scored_item)
        payload.append(row)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_run_stats(stats: dict[str, Any], output_dir: Path) -> None:
    (output_dir / "run_stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_rubric(args: argparse.Namespace) -> str:
    parts: list[str] = []
    if args.rubric_file:
        parts.append(Path(args.rubric_file).read_text(encoding="utf-8").strip())
    if args.rubric:
        parts.append(args.rubric.strip())
    return "\n\n".join(part for part in parts if part) or (
        "偏好高信息密度、原创洞察、数据/案例充分、可沉淀为方法论的内容；"
        "不喜欢标题党、广告、低质量转载、短平快新闻噪音。"
    )


def build_arg_parser() -> argparse.ArgumentParser:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="抓取 RSS/Atom/OPML，用 DeepSeek 评分，并导出 Reeder 友好的结果。"
    )
    parser.add_argument("input", help="RSS/Atom URL、OPML 文件、每行一个 URL 的文本文件，或 - 表示 stdin")
    parser.add_argument("--output-dir", default="output", help="输出目录，默认 output")
    parser.add_argument("--min-score", type=int, default=75, help="进入高分清单的最低分，默认 75")
    parser.add_argument("--max-per-feed", type=int, default=12, help="每个源最多抓取多少篇，默认 12")
    parser.add_argument("--limit", type=int, default=40, help="最终最多输出多少篇，默认 40")
    parser.add_argument("--days", type=int, default=21, help="只看最近多少天；0 表示不过滤，默认 21")
    parser.add_argument("--since", default="", help="只看此 ISO 8601 时间之后发布的文章")
    parser.add_argument("--today", action="store_true", help="只看指定时区当天 00:00 之后发布的文章")
    parser.add_argument(
        "--today-timezone",
        default="Asia/Shanghai",
        help="--today 使用的 IANA 时区，默认 Asia/Shanghai",
    )
    parser.add_argument("--batch-size", type=int, default=8, help="每次发给 DeepSeek 的条目数，默认 8")
    parser.add_argument("--sleep", type=float, default=0.4, help="DeepSeek 批次间隔秒数，默认 0.4")
    parser.add_argument("--model", default=os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL), help=f"DeepSeek 模型，默认 {DEFAULT_MODEL}")
    parser.add_argument("--api-key", default=os.getenv("DEEPSEEK_API_KEY"), help="DeepSeek API Key；也可用环境变量 DEEPSEEK_API_KEY")
    parser.add_argument("--rubric", default="", help="你的阅读偏好/评分标准")
    parser.add_argument("--rubric-file", default="", help="评分标准文件，适合长期维护偏好")
    parser.add_argument("--source-url", default="", help="当输入是本地 RSS 文件时，用这个原始 URL 写入导出的 OPML")
    parser.add_argument("--channel-link", default="", help="导出 RSS 的公开订阅地址")
    parser.add_argument("--include-url-path", action="append", default=[], help="只保留链接路径包含该片段的条目，可重复传入")
    parser.add_argument("--include-title-prefix", action="append", default=[], help="只保留标题以该前缀开头的条目，可重复传入")
    parser.add_argument("--fetch-pages", action="store_true", help="抓取每篇文章页面正文，再交给 DeepSeek 评分和总结")
    parser.add_argument("--page-char-limit", type=int, default=8000, help="每篇文章最多送入 DeepSeek 的页面文本长度，默认 8000 字符")
    parser.add_argument("--page-timeout", type=int, default=10, help="抓取文章页面的超时时间，默认 10 秒")
    parser.add_argument("--page-retries", type=int, default=1, help="页面抓取失败后的重试次数，默认 1")
    parser.add_argument("--page-workers", type=int, default=1, help="并发抓取文章页面的数量，默认 1")
    parser.add_argument("--no-reeder-unique", action="store_true", help="不要给 RSS 条目的链接和 GUID 加去重标记")
    parser.add_argument("--insecure-feed", action="store_true", help="跳过 RSS 订阅源 HTTPS 证书校验；只影响抓取订阅源，不影响 DeepSeek API")
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    rubric = load_rubric(args)
    source_text = read_input(args.input, insecure_tls=args.insecure_feed)
    sources = parse_sources(args.input, source_text)
    sources = override_single_source_url(sources, args.source_url.strip())
    if not sources:
        print("没有找到可用订阅源。", file=sys.stderr)
        return 2

    since = None
    if args.since:
        try:
            since = dt.datetime.fromisoformat(args.since.replace("Z", "+00:00"))
        except ValueError as exc:
            parser.error(f"无法解析 --since：{args.since}")
            raise AssertionError from exc
        if since.tzinfo is None:
            since = since.replace(tzinfo=dt.timezone.utc)
        since = since.astimezone(dt.timezone.utc)
    elif args.today:
        try:
            today_timezone = ZoneInfo(args.today_timezone)
        except ZoneInfoNotFoundError as exc:
            parser.error(f"未知时区：{args.today_timezone}")
            raise AssertionError from exc
        local_now = dt.datetime.now(today_timezone)
        local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        since = local_midnight.astimezone(dt.timezone.utc)
    elif args.days > 0:
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.days)

    run_stats: dict[str, Any] = {
        "since": since.isoformat() if since else "",
        "raw_feed_items": 0,
        "after_time_filter": 0,
        "after_source_cap": 0,
        "after_channel_filter": 0,
        "scored": 0,
        "high_score": 0,
        "page_status": {},
        "errors": 0,
    }
    items: list[FeedItem] = []
    errors: list[str] = []
    for source in sources:
        try:
            input_is_url = urllib.parse.urlparse(args.input).scheme in {"http", "https"}
            use_input_text = len(sources) == 1 and (source.url == args.input or not input_is_url)
            feed_text = source_text if use_input_text else fetch_text(source.url, insecure_tls=args.insecure_feed)
            items.extend(parse_feed(source, feed_text, args.max_per_feed, since, run_stats))
        except Exception as exc:  # noqa: BLE001 - keep going across many feeds.
            errors.append(f"{source.url}: {exc}")

    if not items:
        run_stats["errors"] = len(errors)
        write_run_stats(run_stats, output_dir)
        print("没有抓到可评分的文章。", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 2

    items = filter_items(items, args.include_url_path, args.include_title_prefix)
    run_stats["after_channel_filter"] = len(items)
    if not items:
        run_stats["errors"] = len(errors)
        write_run_stats(run_stats, output_dir)
        print("过滤后没有可评分的文章。", file=sys.stderr)
        return 2

    if args.fetch_pages:
        page_errors = enrich_items_with_pages(
            items=items,
            page_char_limit=max(500, args.page_char_limit),
            timeout=max(5, args.page_timeout),
            insecure_tls=args.insecure_feed,
            sleep_seconds=max(0, min(args.sleep, 2)),
            retries=max(0, min(args.page_retries, 3)),
            workers=max(1, min(args.page_workers, 8)),
        )
        errors.extend(page_errors)

    page_statuses: dict[str, int] = {}
    for item in items:
        page_statuses[item.page_status] = page_statuses.get(item.page_status, 0) + 1
    run_stats["page_status"] = page_statuses

    scored = score_items(
        items=items,
        rubric=rubric,
        api_key=args.api_key,
        model=args.model,
        batch_size=max(1, args.batch_size),
        sleep_seconds=max(0, args.sleep),
    )
    scored.sort(key=lambda row: row.score, reverse=True)
    selected = [row for row in scored if row.score >= args.min_score][: args.limit]
    run_stats["scored"] = len(scored)
    run_stats["high_score"] = len(selected)
    run_stats["errors"] = len(errors)

    write_rss(
        selected,
        output_dir / "high_score.xml",
        "DeepSeek 高分 RSS",
        args.min_score,
        unique_for_reeder=not args.no_reeder_unique,
        channel_link=args.channel_link or "http://127.0.0.1/rss-reeder-ranker",
    )
    write_markdown(selected, output_dir / "top_articles.md", args.min_score)
    write_markdown(scored, output_dir / "all_articles.md", 0, title="DeepSeek RSS 全量评分与总结")
    write_opml(selected, output_dir / "reeder_high_score_sources.opml")
    write_json(scored, output_dir / "scored_items.json")
    write_run_stats(run_stats, output_dir)

    print(f"订阅源：{len(sources)} 个")
    print(f"候选文章：{len(items)} 篇")
    print(f"高分文章：{len(selected)} 篇")
    print(f"输出目录：{output_dir.resolve()}")
    if errors:
        print("部分订阅源抓取失败：", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
    if not args.api_key:
        print("提示：未设置 DEEPSEEK_API_KEY，本次使用本地规则预览评分。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
