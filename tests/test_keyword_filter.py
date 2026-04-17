"""Tests for the pre-LLM keyword blocklist filter."""

import pytest

from src.keyword_filter import (
    compile_keyword_pattern,
    filter_blocked_keywords,
    find_blocked_match,
)


class TestCompileKeywordPattern:
    def test_empty_returns_none(self):
        assert compile_keyword_pattern([]) is None
        assert compile_keyword_pattern(["", "  "]) is None

    def test_single_keyword(self):
        pat = compile_keyword_pattern(["staking"])
        assert pat.search("I love staking") is not None
        assert pat.search("Restaking protocols") is None  # whole-word

    def test_case_insensitive(self):
        pat = compile_keyword_pattern(["Client diversity"])
        assert pat.search("client DIVERSITY is low") is not None
        assert pat.search("CLIENT DIVERSITY update") is not None

    def test_regex_special_chars_escaped(self):
        # Special chars must be escaped so compilation doesn't crash — even when
        # the \b boundary makes them unmatchable. The `.` here would otherwise
        # match any char, but escaped + word-boundary means "exactly a.b".
        pat = compile_keyword_pattern(["a.b"])
        assert pat.search("this is a.b here") is not None
        assert pat.search("this is aXb here") is None

    def test_multi_word_phrase_boundary(self):
        pat = compile_keyword_pattern(["Client diversity"])
        assert pat.search("Client diversity:") is not None
        assert pat.search("(Client diversity)") is not None
        assert pat.search("no-client diversity here") is not None  # word-boundary at 'c'


class TestFindBlockedMatch:
    def test_matches_in_text(self):
        pat = compile_keyword_pattern(["staking"])
        item = {"text": "Liquid staking update"}
        assert find_blocked_match(item, pat) == "staking"

    def test_matches_in_article_title(self):
        pat = compile_keyword_pattern(["marketshare"])
        item = {"text": "", "article": {"title": "Staking marketshare report", "body": ""}}
        assert find_blocked_match(item, pat) == "marketshare"

    def test_matches_in_article_body(self):
        pat = compile_keyword_pattern(["client diversity"])
        item = {
            "text": "",
            "article": {"title": "Ethereum weekly", "body": "Client diversity: Lighthouse 53%"},
        }
        assert find_blocked_match(item, pat) == "Client diversity"

    def test_matches_in_quoted_tweet(self):
        pat = compile_keyword_pattern(["staking"])
        item = {"text": "nice", "quoted_tweet": {"text": "liquid staking analysis"}}
        assert find_blocked_match(item, pat) == "staking"

    def test_no_match_returns_none(self):
        pat = compile_keyword_pattern(["staking"])
        item = {"text": "just a tweet about rollups"}
        assert find_blocked_match(item, pat) is None

    def test_tolerates_missing_fields(self):
        pat = compile_keyword_pattern(["foo"])
        # No 'text' key, no 'article', no 'quoted_tweet'
        assert find_blocked_match({}, pat) is None


class TestFilterBlockedKeywords:
    def test_no_keywords_returns_all_kept(self):
        items = [{"text": "hi", "author_username": "a"}]
        kept, blocked = filter_blocked_keywords(items, [])
        assert kept == items
        assert blocked == []

    def test_splits_by_keyword_match(self):
        items = [
            {"tweet_id": "1", "text": "Client diversity is low", "author_username": "a"},
            {"tweet_id": "2", "text": "Rollup sequencer deep dive", "author_username": "b"},
        ]
        kept, blocked = filter_blocked_keywords(items, ["Client diversity"])
        assert [t["tweet_id"] for t in kept] == ["2"]
        assert [t["tweet_id"] for t in blocked] == ["1"]
        assert blocked[0]["blocked_keyword"] == "Client diversity"

    def test_exempt_authors_always_kept(self):
        items = [
            {"tweet_id": "1", "text": "Client diversity snippet", "author_username": "starred"},
            {"tweet_id": "2", "text": "Client diversity snippet", "author_username": "other"},
        ]
        kept, blocked = filter_blocked_keywords(
            items, ["client diversity"], exempt_authors={"starred"},
        )
        assert [t["tweet_id"] for t in kept] == ["1"]
        assert [t["tweet_id"] for t in blocked] == ["2"]

    def test_exempt_authors_case_insensitive(self):
        items = [{"tweet_id": "1", "text": "Client diversity", "author_username": "StarredUser"}]
        kept, blocked = filter_blocked_keywords(
            items, ["client diversity"], exempt_authors={"starreduser"},
        )
        assert len(kept) == 1
        assert len(blocked) == 0

    def test_substring_inside_word_not_blocked(self):
        # "staking" must not match inside "restaking"
        items = [{"tweet_id": "1", "text": "Restaking protocols hit $20B", "author_username": "a"}]
        kept, blocked = filter_blocked_keywords(items, ["staking"])
        assert len(kept) == 1
        assert len(blocked) == 0
