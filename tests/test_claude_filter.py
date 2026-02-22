"""Tests for ClaudeFilter."""

import json
from unittest.mock import MagicMock, patch

import pytest

from src.claude_filter import ClaudeFilter


@pytest.fixture
def claude_filter():
    """ClaudeFilter with mocked Anthropic client."""
    with patch("src.claude_filter.Anthropic"):
        cf = ClaudeFilter(api_key="fake-key")
    return cf


# --- _parse_response ---

class TestParseResponse:
    def test_clean_json(self, claude_filter):
        raw = json.dumps([
            {"tweet_id": "1", "score": 90, "reason": "Great technical content"},
            {"tweet_id": "2", "score": 40, "reason": "Not relevant"},
        ])
        result = claude_filter._parse_response(raw)
        assert len(result) == 2
        assert result[0] == {"tweet_id": "1", "score": 90, "reason": "Great technical content"}
        assert result[1] == {"tweet_id": "2", "score": 40, "reason": "Not relevant"}

    def test_markdown_wrapped_json(self, claude_filter):
        raw = "```json\n" + json.dumps([
            {"tweet_id": "1", "score": 85, "reason": "Relevant"}
        ]) + "\n```"
        result = claude_filter._parse_response(raw)
        assert len(result) == 1
        assert result[0]["tweet_id"] == "1"
        assert result[0]["score"] == 85

    def test_skips_invalid_items(self, claude_filter):
        raw = json.dumps([
            {"tweet_id": "1", "score": 80, "reason": "Good"},
            "not a dict",
            42,
            {"tweet_id": "2", "score": 60, "reason": "Okay"},
        ])
        result = claude_filter._parse_response(raw)
        assert len(result) == 2
        assert result[0]["tweet_id"] == "1"
        assert result[1]["tweet_id"] == "2"

    def test_skips_missing_required_fields(self, claude_filter):
        raw = json.dumps([
            {"tweet_id": "1", "score": 80, "reason": "Good"},
            {"tweet_id": "2"},  # missing score
            {"score": 50},  # missing tweet_id
            {"reason": "orphan"},  # missing both
        ])
        result = claude_filter._parse_response(raw)
        assert len(result) == 1
        assert result[0]["tweet_id"] == "1"

    def test_missing_reason_gets_default(self, claude_filter):
        raw = json.dumps([{"tweet_id": "1", "score": 75}])
        result = claude_filter._parse_response(raw)
        assert result[0]["reason"] == "No reason provided"

    def test_tweet_id_coerced_to_string(self, claude_filter):
        raw = json.dumps([{"tweet_id": 12345, "score": 80, "reason": "ok"}])
        result = claude_filter._parse_response(raw)
        assert result[0]["tweet_id"] == "12345"

    def test_invalid_json_falls_back(self, claude_filter):
        raw = 'Here are the scores: {"tweet_id": "1", "score": 88, "reason": "good"} and more text'
        result = claude_filter._parse_response(raw)
        # Should fall back to _fallback_parse
        assert len(result) == 1
        assert result[0]["tweet_id"] == "1"
        assert result[0]["score"] == 88


# --- _fallback_parse ---

class TestFallbackParse:
    def test_extracts_from_malformed_text(self, claude_filter):
        raw = (
            'Here are my scores:\n'
            '{"tweet_id": "abc", "score": 92, "reason": "based rollup"}\n'
            'some garbage\n'
            '{"tweet_id": "def", "score": 45, "reason": "not relevant"}\n'
        )
        result = claude_filter._fallback_parse(raw)
        assert len(result) == 2
        assert result[0] == {"tweet_id": "abc", "score": 92, "reason": "based rollup"}
        assert result[1] == {"tweet_id": "def", "score": 45, "reason": "not relevant"}

    def test_no_matches_returns_empty(self, claude_filter):
        result = claude_filter._fallback_parse("Sorry, I can't score these tweets.")
        assert result == []


# --- filter_tweets ---

class TestFilterTweets:
    def test_empty_input(self, claude_filter):
        result = claude_filter.filter_tweets([])
        assert result == []

    def test_threshold_filtering(self, claude_filter, sample_tweets):
        scores_json = json.dumps([
            {"tweet_id": "123456789", "score": 85, "reason": "EIP discussion"},
            {"tweet_id": "987654321", "score": 92, "reason": "Based rollup"},
            {"tweet_id": "111222333", "score": 10, "reason": "Meme coin"},
        ])
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=scores_json)]
        claude_filter.client.messages.create.return_value = mock_response

        result = claude_filter.filter_tweets(sample_tweets, threshold=70)

        assert len(result) == 2
        tweet_ids = {t["tweet_id"] for t in result}
        assert "123456789" in tweet_ids
        assert "987654321" in tweet_ids
        assert "111222333" not in tweet_ids

    def test_score_mapping_to_tweets(self, claude_filter, sample_tweet):
        scores_json = json.dumps([
            {"tweet_id": "123456789", "score": 85, "reason": "EIP discussion"},
        ])
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=scores_json)]
        claude_filter.client.messages.create.return_value = mock_response

        result = claude_filter.filter_tweets([sample_tweet], threshold=70)

        assert len(result) == 1
        assert result[0]["filter_score"] == 85
        assert result[0]["filter_reason"] == "EIP discussion"
        assert result[0]["filtered"] is True

    def test_unscored_tweet_defaults_to_skip(self, claude_filter, sample_tweet):
        # Claude returns empty scores
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="[]")]
        claude_filter.client.messages.create.return_value = mock_response

        result = claude_filter.filter_tweets([sample_tweet], threshold=70)

        assert result == []
        assert sample_tweet["filter_score"] == 0
        assert sample_tweet["filtered"] is False

    def test_api_error_propagates(self, claude_filter, sample_tweet):
        claude_filter.client.messages.create.side_effect = Exception("API error")

        with pytest.raises(Exception, match="API error"):
            claude_filter.filter_tweets([sample_tweet])

    def test_rag_context_uses_v2_prompt(self, claude_filter, sample_tweet):
        scores_json = json.dumps([
            {"tweet_id": "123456789", "score": 90, "reason": "Boosted by RAG"},
        ])
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=scores_json)]
        claude_filter.client.messages.create.return_value = mock_response

        rag_context = (
            'Liked tweets (boost similar content):\n'
            '- @vitalik: "EIP blob market" (similarity: 0.92)\n'
        )
        result = claude_filter.filter_tweets(
            [sample_tweet], threshold=70, rag_context=rag_context
        )

        assert len(result) == 1
        assert result[0]["filter_score"] == 90

        # Verify V2 prompt was used (contains "User Feedback Context")
        call_args = claude_filter.client.messages.create.call_args
        prompt_text = call_args[1]["messages"][0]["content"]
        assert "User Feedback Context" in prompt_text
        assert "EIP blob market" in prompt_text

    def test_no_rag_context_uses_v1_prompt(self, claude_filter, sample_tweet):
        scores_json = json.dumps([
            {"tweet_id": "123456789", "score": 85, "reason": "Good"},
        ])
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=scores_json)]
        claude_filter.client.messages.create.return_value = mock_response

        result = claude_filter.filter_tweets([sample_tweet], threshold=70)

        call_args = claude_filter.client.messages.create.call_args
        prompt_text = call_args[1]["messages"][0]["content"]
        assert "User Feedback Context" not in prompt_text

    def test_quoted_tweet_included_in_claude_input(self, claude_filter, sample_quote_tweet):
        scores_json = json.dumps([
            {"tweet_id": "555666777", "score": 90, "reason": "Quote of blob analysis"},
        ])
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=scores_json)]
        claude_filter.client.messages.create.return_value = mock_response

        result = claude_filter.filter_tweets([sample_quote_tweet], threshold=70)

        assert len(result) == 1
        # Verify the prompt sent to Claude includes quoted_tweet
        call_args = claude_filter.client.messages.create.call_args
        prompt_text = call_args[1]["messages"][0]["content"]
        assert "quoted_tweet" in prompt_text
        assert "vitalikbuterin" in prompt_text
        assert "blob fee market" in prompt_text

    def test_no_quoted_tweet_omits_field(self, claude_filter, sample_tweet):
        scores_json = json.dumps([
            {"tweet_id": "123456789", "score": 85, "reason": "Good"},
        ])
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=scores_json)]
        claude_filter.client.messages.create.return_value = mock_response

        claude_filter.filter_tweets([sample_tweet], threshold=70)

        call_args = claude_filter.client.messages.create.call_args
        prompt_text = call_args[1]["messages"][0]["content"]
        # quoted_tweet key should not appear for tweets without one
        assert "quoted_tweet" not in prompt_text

    def test_high_threshold(self, claude_filter, sample_tweets):
        scores_json = json.dumps([
            {"tweet_id": "123456789", "score": 85, "reason": "EIP discussion"},
            {"tweet_id": "987654321", "score": 92, "reason": "Based rollup"},
            {"tweet_id": "111222333", "score": 10, "reason": "Meme coin"},
        ])
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=scores_json)]
        claude_filter.client.messages.create.return_value = mock_response

        result = claude_filter.filter_tweets(sample_tweets, threshold=90)

        assert len(result) == 1
        assert result[0]["tweet_id"] == "987654321"
