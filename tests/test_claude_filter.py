"""Tests for ClaudeFilter."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.claude_filter import (
    DEFAULT_PROFILE,
    EXTRACT_TOOL,
    SCORE_TOOL,
    ClaudeFilter,
)


@pytest.fixture
def claude_filter():
    """ClaudeFilter with mocked Anthropic client."""
    with patch("src.claude_filter.Anthropic"):
        cf = ClaudeFilter(api_key="fake-key")
    return cf


def tool_response(scores: list[dict]):
    """Build a fake Messages response carrying a submit_scores tool call."""
    block = SimpleNamespace(type="tool_use", name="submit_scores", input={"scores": scores})
    return SimpleNamespace(content=[block], stop_reason="tool_use")


def prompt_sent(claude_filter) -> str:
    call = claude_filter.client.messages.create.call_args
    return call[1]["messages"][0]["content"]


# --- _extract_scores ---

class TestExtractScores:
    def test_extracts_valid_scores(self):
        resp = tool_response([
            {"item_id": "1", "score": 85, "reason": "Relevant"},
            {"item_id": "2", "score": 10, "reason": "Meme"},
        ])
        result = ClaudeFilter._extract_scores(resp, {"1", "2"})
        assert result == [
            {"item_id": "1", "score": 85, "reason": "Relevant", "domain": "other"},
            {"item_id": "2", "score": 10, "reason": "Meme", "domain": "other"},
        ]

    def test_domain_kept_when_known(self):
        resp = tool_response([{"item_id": "1", "score": 50, "reason": "r", "domain": "ai"},
                              {"item_id": "2", "score": 50, "reason": "r", "domain": "made-up"}])
        result = ClaudeFilter._extract_scores(resp, {"1", "2"})
        assert [r["domain"] for r in result] == ["ai", "other"]

    def test_drops_unknown_and_duplicate_ids(self):
        resp = tool_response([
            {"item_id": "1", "score": 85, "reason": "a"},
            {"item_id": "1", "score": 20, "reason": "dupe"},
            {"item_id": "999", "score": 90, "reason": "hallucinated"},
        ])
        result = ClaudeFilter._extract_scores(resp, {"1", "2"})
        assert [r["item_id"] for r in result] == ["1"]
        assert result[0]["score"] == 85

    def test_clamps_and_coerces(self):
        resp = tool_response([
            {"item_id": 1, "score": 150, "reason": ""},
            {"item_id": "2", "score": "abc", "reason": "x"},
        ])
        result = ClaudeFilter._extract_scores(resp, {"1", "2"})
        assert result == [{"item_id": "1", "score": 100, "reason": "No reason provided", "domain": "other"}]

    def test_no_tool_block_returns_empty(self):
        resp = SimpleNamespace(content=[SimpleNamespace(type="text", text="sorry")])
        assert ClaudeFilter._extract_scores(resp, {"1"}) == []


# --- filter_tweets ---

class TestFilterTweets:
    def test_empty_input(self, claude_filter):
        assert claude_filter.filter_tweets([]) == []
        claude_filter.client.messages.create.assert_not_called()

    def test_threshold_filtering(self, claude_filter, sample_tweets):
        claude_filter.client.messages.create.return_value = tool_response([
            {"item_id": "123456789", "score": 85, "reason": "EIP discussion"},
            {"item_id": "987654321", "score": 92, "reason": "Based rollup"},
            {"item_id": "111222333", "score": 10, "reason": "Meme coin"},
        ])

        result = claude_filter.filter_tweets(sample_tweets, threshold=70)

        assert {t["item_id"] for t in result} == {"123456789", "987654321"}
        assert all(t["domain"] == "other" for t in sample_tweets)
        # All inputs annotated in place
        meme = next(t for t in sample_tweets if t["item_id"] == "111222333")
        assert meme["filter_score"] == 10
        assert meme["filtered"] is False

    def test_unscored_tweet_defaults_to_skip(self, claude_filter, sample_tweet):
        claude_filter.client.messages.create.return_value = tool_response([])

        result = claude_filter.filter_tweets([sample_tweet], threshold=70)

        assert result == []
        assert sample_tweet["filter_score"] is None  # not persisted as scored → retried
        assert sample_tweet["filter_reason"] == "Not scored by Claude"
        assert sample_tweet["filtered"] is False

    def test_missing_tool_call_retries_once_with_nudge(self, claude_filter, sample_tweet):
        text_only = SimpleNamespace(content=[SimpleNamespace(type="text", text="Here are the scores…")])
        claude_filter.client.messages.create.side_effect = [
            text_only, tool_response([{"item_id": "123456789", "score": 75, "reason": "ok"}]),
        ]
        claude_filter.filter_tweets([sample_tweet], threshold=70)
        assert sample_tweet["filter_score"] == 75
        calls = claude_filter.client.messages.create.call_args_list
        assert len(calls) == 2
        assert "Respond only by calling the submit_scores tool" in calls[1][1]["messages"][0]["content"]

    def test_second_missing_tool_call_leaves_batch_unscored(self, claude_filter, sample_tweet):
        text_only = SimpleNamespace(content=[SimpleNamespace(type="text", text="no")])
        claude_filter.client.messages.create.side_effect = [text_only, text_only]
        assert claude_filter.filter_tweets([sample_tweet], threshold=70) == []
        assert sample_tweet["filter_score"] is None

    def test_api_error_propagates(self, claude_filter, sample_tweet):
        claude_filter.client.messages.create.side_effect = Exception("API error")
        with pytest.raises(Exception, match="API error"):
            claude_filter.filter_tweets([sample_tweet])

    def test_uses_tool_choice_and_schema(self, claude_filter, sample_tweet):
        claude_filter.client.messages.create.return_value = tool_response([
            {"item_id": "123456789", "score": 80, "reason": "ok"},
        ])
        claude_filter.filter_tweets([sample_tweet])

        kwargs = claude_filter.client.messages.create.call_args[1]
        assert kwargs["tools"] == [SCORE_TOOL]
        assert SCORE_TOOL["strict"] is True
        # Claude 5.5+ rejects forced tool_choice and non-default temperature
        assert kwargs["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
        assert "temperature" not in kwargs
        assert kwargs["output_config"] == {"effort": "medium"}
        assert kwargs["model"] == "claude-sonnet-5-5"

    def test_default_profile_in_prompt(self, claude_filter, sample_tweet):
        claude_filter.client.messages.create.return_value = tool_response([])
        claude_filter.filter_tweets([sample_tweet])
        assert DEFAULT_PROFILE.splitlines()[0] in prompt_sent(claude_filter)

    def test_profile_override(self, claude_filter, sample_tweet):
        claude_filter.client.messages.create.return_value = tool_response([])
        claude_filter.filter_tweets([sample_tweet], profile="Loves film criticism.")
        prompt = prompt_sent(claude_filter)
        assert "Loves film criticism." in prompt
        assert "Based rollups" not in prompt

    def test_neighbors_attached_per_item(self, claude_filter, sample_tweets):
        claude_filter.client.messages.create.return_value = tool_response([])
        neighbors = {
            "123456789": [
                {"item_id": "x", "text": "blob fee analysis", "author_username": "dankrad",
                 "vote": "up", "similarity": 0.91},
            ],
        }
        claude_filter.filter_tweets(sample_tweets, neighbors=neighbors)

        prompt = prompt_sent(claude_filter)
        items = json.loads(prompt.split("## Items\n", 1)[1].rsplit("\n\nScore every item", 1)[0])
        by_id = {i["item_id"]: i for i in items}
        assert by_id["123456789"]["similar_past_votes"] == [
            {"vote": "up", "similarity": 0.91, "author": "dankrad", "text": "blob fee analysis"},
        ]
        assert "similar_past_votes" not in by_id["987654321"]

    def test_quoted_tweet_and_article_included(self, claude_filter, sample_quote_tweet):
        claude_filter.client.messages.create.return_value = tool_response([])
        sample_quote_tweet["article"] = {"title": "Long read", "body": "body text"}
        claude_filter.filter_tweets([sample_quote_tweet])

        prompt = prompt_sent(claude_filter)
        assert "Deep dive into blob fee market" in prompt
        assert "Long read" in prompt

    def test_batches_large_input(self, claude_filter):
        claude_filter.batch_size = 2
        tweets = [
            {"item_id": str(i), "author_username": "a", "text": f"t{i}", "metrics": {}}
            for i in range(5)
        ]
        claude_filter.client.messages.create.side_effect = [
            tool_response([{"item_id": "0", "score": 90, "reason": ""}, {"item_id": "1", "score": 90, "reason": ""}]),
            tool_response([{"item_id": "2", "score": 10, "reason": ""}, {"item_id": "3", "score": 90, "reason": ""}]),
            tool_response([{"item_id": "4", "score": 90, "reason": ""}]),
        ]

        result = claude_filter.filter_tweets(tweets, threshold=70)

        assert claude_filter.client.messages.create.call_count == 3
        assert {t["item_id"] for t in result} == {"0", "1", "3", "4"}


# --- build_item_entry ---

class TestBuildItemEntry:
    def test_minimal_entry(self):
        entry = ClaudeFilter.build_item_entry({"item_id": "1", "author_username": "a", "text": "hi"})
        assert entry == {"item_id": "1", "author": "a", "text": "hi", "likes": 0, "retweets": 0}

    def test_blog_post_type_and_truncated_neighbor_text(self):
        entry = ClaudeFilter.build_item_entry(
            {"item_id": "b", "author_username": "blog", "text": "post", "content_type": "blog_post"},
            neighbors=[{"vote": "down", "similarity": 0.7, "author_username": "z", "text": "x" * 500,
                        "reason": "Event/promo"}],
        )
        assert entry["type"] == "blog_post"
        n = entry["similar_past_votes"][0]
        assert len(n["text"]) == 200
        assert n["reason"] == "Event/promo"


# --- extract_newsletter_entries ---

class TestExtractNewsletterEntries:
    def test_filters_and_dedupes(self, claude_filter):
        block = SimpleNamespace(type="tool_use", name="extract_entries", input={"entries": [
            {"title": "A", "url": "https://a.example/post", "summary": "s", "author": "", "section": "Research"},
            {"title": "dup", "url": "https://a.example/post"},
            {"title": "bad", "url": "mailto:x@y"},
            {"title": "B", "url": "https://b.example"},
        ]})
        claude_filter.client.messages.create.return_value = SimpleNamespace(content=[block])

        entries = claude_filter.extract_newsletter_entries("Issue 1", "page text")

        assert [e["url"] for e in entries] == ["https://a.example/post", "https://b.example"]
        assert entries[0]["author"] is None and entries[0]["section"] == "Research"
        kwargs = claude_filter.client.messages.create.call_args[1]
        assert kwargs["tools"] == [EXTRACT_TOOL]
        assert "Issue 1" in kwargs["messages"][0]["content"]

    def test_no_tool_block_raises_after_retry(self, claude_filter):
        from src.claude_filter import NoToolCallError
        claude_filter.client.messages.create.return_value = SimpleNamespace(content=[])
        with pytest.raises(NoToolCallError):
            claude_filter.extract_newsletter_entries("t", "x")
        assert claude_filter.client.messages.create.call_count == 2
