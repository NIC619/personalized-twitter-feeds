"""Tests for the agent-facing preference API."""

from unittest.mock import MagicMock

import pytest

from src.claude_filter import DEFAULT_PROFILE
from src.preference_api import PreferenceAPI, item_id_for


@pytest.fixture
def db():
    db = MagicMock()
    db.get_latest_profiles.return_value = {}
    db.get_item.return_value = None
    db.get_item_by_url.return_value = None
    db.has_embedding.return_value = False
    return db


class TestItemIdFor:
    def test_url(self):
        assert item_id_for("https://a.b/c", None).startswith("blog_")

    def test_text(self):
        assert item_id_for(None, "hello").startswith("note_")
        assert item_id_for(None, "hello") == item_id_for(None, "  Hello ")


class TestGetProfile:
    def test_fallback_document(self, db):
        text = PreferenceAPI(db).get_profile()
        assert "No compiled profile yet" in text and DEFAULT_PROFILE[:20] in text

    def test_domain(self, db):
        db.get_latest_profiles.return_value = {"ai": {"content": "AI PROFILE", "version": 1, "stats": {}}}
        api = PreferenceAPI(db)
        assert api.get_profile("ai") == "AI PROFILE"
        assert "No compiled profile for domain 'culture'" in api.get_profile("culture")

    def test_full_document(self, db):
        db.get_latest_profiles.return_value = {"ai": {"content": "AI PROFILE", "version": 1, "stats": {}}}
        assert PreferenceAPI(db).get_profile().startswith("# Preferences")


class TestSearchLiked:
    def test_semantic_path(self, db):
        emb = MagicMock(); emb.enabled = True
        emb.generate_embedding.return_value = [0.1]
        db.find_similar_voted.return_value = [
            {"item_id": "1", "vote": "up", "strength": 2, "reason": "r", "similarity": 0.91, "domain": "ai"},
            {"item_id": "2", "vote": "down", "strength": 1, "reason": None, "similarity": 0.80, "domain": "crypto"},
        ]
        db.get_items.return_value = {"1": {"item_id": "1", "text": "one", "url": "u1", "domain": "ai"}}

        out = PreferenceAPI(db, emb).search_liked("agents", limit=5)

        assert out[0]["item_id"] == "1" and out[0]["vote"] == "up" and out[0]["similarity"] == 0.91
        assert out[1]["item_id"] == "2"
        db.find_similar_voted.assert_called_once_with([0.1], limit=5)

    def test_semantic_path_domain_filter(self, db):
        emb = MagicMock(); emb.enabled = True
        emb.generate_embedding.return_value = [0.1]
        db.find_similar_voted.return_value = [
            {"item_id": "1", "vote": "up", "similarity": 0.9, "domain": "ai"},
            {"item_id": "2", "vote": "up", "similarity": 0.8, "domain": "crypto"},
        ]
        db.get_items.return_value = {}
        out = PreferenceAPI(db, emb).search_liked("x", domain="crypto", limit=5)
        assert [o["item_id"] for o in out] == ["2"]

    def test_text_fallback_only_voted(self, db):
        db.search_items_text.return_value = [{"item_id": "1", "text": "a"}, {"item_id": "2", "text": "b"}]
        db.get_labeled_items.return_value = [{"item_id": "2", "user_vote": "up", "vote_strength": 1, "vote_reason": None}]
        out = PreferenceAPI(db, None).search_liked("x")
        assert [o["item_id"] for o in out] == ["2"]


class TestScoreItems:
    def test_requires_claude(self, db):
        with pytest.raises(RuntimeError):
            PreferenceAPI(db).score_items([{"text": "x"}])

    def test_scores_in_order(self, db):
        claude = MagicMock()
        claude.score_tweets.side_effect = lambda prepared, **kw: [
            {"item_id": prepared[1]["item_id"], "score": 80, "reason": "b", "domain": "ai"},
        ]
        out = PreferenceAPI(db, None, claude).score_items([{"title": "A"}, {"text": "B", "url": "https://x/y"}])
        assert out[0]["score"] is None
        assert out[1]["score"] == 80 and out[1]["domain"] == "ai"
        prepared = claude.score_tweets.call_args[0][0]
        assert prepared[0]["text"] == "A"


class TestRecordSignal:
    def test_validation(self, db):
        api = PreferenceAPI(db)
        with pytest.raises(ValueError):
            api.record_signal(vote="sideways", url="https://a/b")
        with pytest.raises(ValueError):
            api.record_signal(url="https://a/b")  # neither vote nor note
        with pytest.raises(ValueError):
            api.record_signal(vote="up", url="ftp://nope")
        with pytest.raises(ValueError):
            api.record_signal(vote="up")  # nothing identifies the item

    def test_creates_item_and_vote(self, db):
        out = PreferenceAPI(db).record_signal(
            vote="up", url="https://a/b", title="T", strength=2, reason="why", source="claude-code",
        )
        saved = db.save_items.call_args[0][0][0]
        assert saved["url"] == "https://a/b" and saved["title"] == "T" and saved["source"] == "claude-code"
        kwargs = db.save_signal.call_args[1]
        assert kwargs["value"] == "up" and kwargs["strength"] == 2 and kwargs["reason"] == "why"
        assert out["written"] == ["vote"] and out["item_id"] == saved["item_id"]

    def test_existing_item_by_url_gets_note(self, db):
        db.get_item_by_url.return_value = {"item_id": "existing", "url": "https://a/b", "text": "t"}
        out = PreferenceAPI(db).record_signal(url="https://a/b", note="remember")
        db.save_items.assert_not_called()
        assert db.save_signal.call_args[1]["kind"] == "note"
        assert out == {"item_id": "existing", "written": ["note"], "url": "https://a/b"}

    def test_down_vote_strength_clamped_and_embedding(self, db):
        emb = MagicMock(); emb.enabled = True
        emb.generate_embedding.return_value = [0.5]
        PreferenceAPI(db, emb).record_signal(vote="down", text="some text here", strength=2)
        assert db.save_signal.call_args[1]["strength"] == 1
        db.save_embedding.assert_called_once()

    def test_unknown_item_id(self, db):
        with pytest.raises(ValueError, match="unknown item_id"):
            PreferenceAPI(db).record_signal(vote="up", item_id="nope")
