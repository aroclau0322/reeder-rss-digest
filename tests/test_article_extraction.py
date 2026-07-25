import json
import tempfile
import unittest
from pathlib import Path

from rss_reeder_ranker import extract_article_text
from update_daily import remove_public_page_text


class ArticleExtractionTests(unittest.TestCase):
    def test_prefers_article_content_and_ignores_page_chrome(self) -> None:
        body = "这是公开文章的正文段落，包含事实、分析和案例。" * 30
        source = (
            "<html><header>导航噪音</header><div class='article-detail'>"
            f"<div class='article__content'><p>{body}</p><script>脚本噪音</script></div>"
            "<footer>页脚噪音</footer></div></html>"
        )
        text, status = extract_article_text(source, 5000, download_complete=True)
        self.assertEqual(status, "full")
        self.assertIn("公开文章的正文段落", text)
        self.assertNotIn("导航噪音", text)
        self.assertNotIn("脚本噪音", text)

    def test_marks_long_article_as_partial(self) -> None:
        body = "长文章正文" * 300
        source = f"<div class='article__content'><p>{body}</p></div>"
        text, status = extract_article_text(source, 500, download_complete=True)
        self.assertEqual(status, "partial")
        self.assertLessEqual(len(text), 501)

    def test_marks_public_preview_as_paywalled(self) -> None:
        body = "公开预览内容" * 40
        source = f"<div class='article__content'><p>{body}</p><p>开通会员阅读全文</p></div>"
        _, status = extract_article_text(source, 5000, download_complete=True)
        self.assertEqual(status, "paywalled")

    def test_public_json_removes_page_text(self) -> None:
        path = Path(tempfile.mkdtemp()) / "scored_items.json"
        path.write_text(
            json.dumps([{"item": {"title": "标题", "page_text": "不应公开", "page_status": "full"}}]),
            encoding="utf-8",
        )
        remove_public_page_text(path)
        item = json.loads(path.read_text(encoding="utf-8"))[0]["item"]
        self.assertNotIn("page_text", item)
        self.assertEqual(item["page_status"], "full")


if __name__ == "__main__":
    unittest.main()
