import datetime as dt
import unittest

from rss_reeder_ranker import FeedItem, FeedSource, ScoredItem, parse_feed
from update_daily import merge_scored_history


def scored_item(
    item_id: str,
    link: str,
    published: str,
    score: int = 80,
) -> ScoredItem:
    return ScoredItem(
        item=FeedItem(
            id=item_id,
            feed_title="测试源",
            feed_url="https://example.com/feed",
            title=item_id,
            link=link,
            summary="摘要",
            published=published,
        ),
        score=score,
        reason="理由",
        optimized_title=item_id,
        tags=[],
        priority="medium",
        ai_summary="总结",
    )


class IncrementalHistoryTests(unittest.TestCase):
    def test_since_filter_is_applied_before_source_cap_and_records_counts(self) -> None:
        feed = """<?xml version="1.0"?>
        <rss><channel><title>测试源</title>
          <item><title>新文章一</title><link>https://example.com/1</link><pubDate>Tue, 04 Aug 2026 20:00:00 +0800</pubDate></item>
          <item><title>新文章二</title><link>https://example.com/2</link><pubDate>Tue, 04 Aug 2026 19:00:00 +0800</pubDate></item>
          <item><title>旧文章</title><link>https://example.com/3</link><pubDate>Sun, 02 Aug 2026 19:00:00 +0800</pubDate></item>
        </channel></rss>"""
        stats: dict[str, int] = {}
        since = dt.datetime(2026, 8, 3, tzinfo=dt.timezone.utc)

        items = parse_feed(FeedSource("测试源", "https://example.com/feed"), feed, 1, since, stats)

        self.assertEqual([item.title for item in items], ["新文章一"])
        self.assertEqual(stats["raw_feed_items"], 3)
        self.assertEqual(stats["after_time_filter"], 2)
        self.assertEqual(stats["after_source_cap"], 1)

    def test_history_prunes_old_items_and_current_score_wins_after_dedupe(self) -> None:
        cutoff = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)
        previous = [
            scored_item("old", "https://example.com/old", "Fri, 31 Jul 2026 10:00:00 +0000"),
            scored_item("duplicate-old", "https://example.com/article?utm_source=rss", "Mon, 03 Aug 2026 10:00:00 +0000", 76),
        ]
        current = [
            scored_item("duplicate-new", "https://example.com/article", "Tue, 04 Aug 2026 10:00:00 +0000", 91),
        ]

        merged = merge_scored_history(previous, current, cutoff)

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].item.id, "duplicate-new")
        self.assertEqual(merged[0].score, 91)


if __name__ == "__main__":
    unittest.main()
