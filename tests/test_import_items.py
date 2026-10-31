"""Tests for the import parsers."""

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "import_items", Path(__file__).resolve().parent.parent / "scripts" / "import_items.py"
)
import_items = importlib.util.module_from_spec(spec)
spec.loader.exec_module(import_items)


class TestVoteSignal:
    def test_created_at_falls_back_to_import_time(self):
        sig = import_items.vote_signal({"item_id": "x", "source": "import:t", "_note": "n"}, "2026-10-31T00:00:00+00:00")
        assert sig["created_at"] == "2026-10-31T00:00:00+00:00" and sig["reason"] == "n"
        assert sig["kind"] == "vote" and sig["value"] == "up"

    def test_item_timestamp_wins(self):
        sig = import_items.vote_signal({"item_id": "x", "source": "s", "created_at": "2024-01-01T00:00:00+00:00"}, "now")
        assert sig["created_at"] == "2024-01-01T00:00:00+00:00"


class TestParseCsv:
    def test_generic_csv(self, tmp_path):
        p = tmp_path / "likes.csv"
        p.write_text(
            "url,title,note,created\n"
            "https://example.com/a,Post A,great,2026-01-02\n"
            "not-a-url,skip me,,\n"
            "https://example.com/b,,,1700000000\n"
        )
        items = import_items.parse_csv(p, "pocket")
        assert [i["url"] for i in items] == ["https://example.com/a", "https://example.com/b"]
        a = items[0]
        assert a["title"] == "Post A" and a["_note"] == "great"
        assert a["source"] == "import:pocket" and a["author_username"] == "example.com"
        assert a["created_at"].startswith("2026-01-02")
        assert items[1]["created_at"].startswith("2023-11-14")
        assert items[1]["text"] == "https://example.com/b"

    def test_raindrop_columns(self, tmp_path):
        p = tmp_path / "raindrop.csv"
        p.write_text(
            "title,note,excerpt,url,folder,tags,created\n"
            "R Title,my note,an excerpt,https://r.io/x,Reading,tag1,2026-03-04T05:06:07.000Z\n"
        )
        items = import_items.parse_csv(p, "raindrop")
        assert items[0]["title"] == "R Title"
        assert items[0]["_note"] == "my note"
        assert "my note" in items[0]["text"]

    def test_bom_header(self, tmp_path):
        p = tmp_path / "bom.csv"
        p.write_text("﻿URL,Title\nhttps://x.y/z,Z\n")
        assert import_items.parse_csv(p, "x")[0]["title"] == "Z"


class TestParseTwitterArchive:
    def test_like_js(self, tmp_path):
        p = tmp_path / "like.js"
        p.write_text(
            'window.YTD.like.part0 = [\n'
            '  {"like": {"tweetId": "111", "fullText": "hello world", "expandedUrl": "https://twitter.com/i/web/status/111"}},\n'
            '  {"like": {"tweetId": "222"}},\n'
            '  {"like": {}}\n'
            ']'
        )
        items = import_items.parse_twitter_archive(p, "nic")
        assert [i["item_id"] for i in items] == ["111", "222"]
        assert items[0]["text"] == "hello world"
        assert items[1]["url"] == "https://x.com/i/web/status/222"
        assert items[0]["source"] == "import:twitter-likes"
        assert items[0]["raw_data"]["liked_by"] == "nic"
