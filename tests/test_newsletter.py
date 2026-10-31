"""Tests for the shared newsletter pipeline (web issue or email → scored items)."""

from unittest.mock import MagicMock

from src.blog_fetcher import BlogFetcher
from src.newsletter import NewsletterProcessor, clean_url


class TestCleanUrl:
    def test_strips_tracking_params_and_fragment(self):
        assert clean_url("https://a.b/p?utm_source=x&id=3&ref=nl#top") == "https://a.b/p?id=3"

    def test_leaves_plain_urls(self):
        assert clean_url("https://a.b/p") == "https://a.b/p"


def _processor(entries, scored_ids=None, resolve=None):
    claude = MagicMock()
    claude.extract_newsletter_entries.return_value = entries
    bf = MagicMock()
    bf.build_post = BlogFetcher.build_post
    bf.resolve_url.side_effect = resolve or (lambda u: u)
    db = MagicMock()
    db.get_scored_ids.return_value = scored_ids or set()
    db.get_items.return_value = {}

    def score(items):
        for it in items:
            it["filter_score"] = 80
        return items
    score = MagicMock(side_effect=score)
    return NewsletterProcessor(claude, bf, db, score=score), claude, bf, db, score


class TestProcess:
    def test_entries_become_scored_items_in_order(self):
        p, claude, bf, db, score = _processor([
            {"title": "A", "url": "https://x.io/a", "summary": "s", "author": None, "section": "Top"},
            {"title": "B", "url": "https://y.io/b?utm_campaign=nl", "summary": "", "author": "Bob", "section": None},
        ])
        result = p.process("Issue 9", "page text", issue_url="https://nl.io/9")

        assert result["entries"] == 2
        ids = [i["item_id"] for i in result["items"]]
        assert ids == [BlogFetcher.build_post(url="https://x.io/a", title="A")["item_id"],
                       BlogFetcher.build_post(url="https://y.io/b", title="B")["item_id"]]
        assert result["items"][1]["url"] == "https://y.io/b"
        assert result["items"][0]["raw_data"]["newsletter_url"] == "https://nl.io/9"
        assert result["items"][0]["raw_data"]["section"] == "Top"
        assert all(i["filter_score"] == 80 for i in result["items"])
        score.assert_called_once()

    def test_known_items_are_not_rescored(self):
        known_id = BlogFetcher.build_post(url="https://x.io/a", title="A")["item_id"]
        p, claude, bf, db, score = _processor(
            [{"title": "A", "url": "https://x.io/a", "summary": "", "author": None, "section": None},
             {"title": "B", "url": "https://x.io/b", "summary": "", "author": None, "section": None}],
            scored_ids={known_id},
        )
        db.get_items.return_value = {known_id: {"item_id": known_id, "filter_score": 30}}
        result = p.process("t", "text")
        assert [i["filter_score"] for i in result["items"]] == [30, 80]
        assert [i["item_id"] for i in score.call_args[0][0]] != [known_id]

    def test_empty_text_or_no_entries(self):
        p, claude, *_ = _processor([])
        assert p.process("t", "   ") == {"title": "t", "entries": 0, "items": []}
        claude.extract_newsletter_entries.assert_not_called()
        assert p.process("t", "some text")["entries"] == 0

    def test_from_email_resolves_tracking_links(self):
        p, claude, bf, db, score = _processor(
            [{"title": "A", "url": "https://substack.com/redirect/abc?j=1", "summary": "", "author": None, "section": None}],
            resolve=lambda u: "https://real.io/post?utm_source=substack",
        )
        result = p.from_email({"subject": "Ethereal #3", "sender": "hi@ethereal.io", "text": "body"})
        item = result["items"][0]
        assert item["url"] == "https://real.io/post"
        assert item["raw_data"]["newsletter_from"] == "hi@ethereal.io"
        assert item["raw_data"]["newsletter_url"] == "mailto:hi@ethereal.io"

    def test_from_email_keeps_link_when_resolution_fails(self):
        p, *_ = _processor(
            [{"title": "A", "url": "https://real.io/a", "summary": "", "author": None, "section": None}],
            resolve=lambda u: None,
        )
        assert p.from_email({"subject": "s", "sender": "", "text": "b"})["items"][0]["url"] == "https://real.io/a"

    def test_from_page(self):
        p, claude, bf, *_ = _processor(
            [{"title": "A", "url": "https://real.io/a", "summary": "", "author": None, "section": None}]
        )
        bf.fetch_page.return_value = {"url": "https://nl.io/1", "title": "NL 1", "text": "body"}
        result = p.from_page("https://nl.io/1")
        assert result["title"] == "NL 1" and result["entries"] == 1
        claude.extract_newsletter_entries.assert_called_once_with("NL 1", "body")

    def test_from_page_unreachable(self):
        p, claude, bf, *_ = _processor([])
        bf.fetch_page.return_value = None
        assert p.from_page("https://nl.io/1")["entries"] == 0
