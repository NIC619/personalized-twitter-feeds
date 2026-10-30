"""Tests for message classification and item construction."""

from types import SimpleNamespace

from src.capture import classify_message, forward_origin, minimal_url_item, text_item


class TestClassifyMessage:
    def test_tweet_url(self):
        c = classify_message("https://x.com/a/status/123")
        assert c.tweet_refs == ["https://x.com/a/status/123"]
        assert not c.urls and not c.usernames and c.text is None

    def test_numeric_ids(self):
        assert classify_message("123 456").tweet_refs == ["123", "456"]

    def test_profile_url_and_handle(self):
        c = classify_message("https://twitter.com/vitalikbuterin")
        assert c.usernames == ["https://twitter.com/vitalikbuterin"]
        assert classify_message("@dankrad").usernames == ["@dankrad"]
        assert classify_message("dankrad").usernames == ["dankrad"]

    def test_other_url_is_capture(self):
        c = classify_message("https://vitalik.eth.limo/general/2024/01/01/foo.html")
        assert c.urls and not c.tweet_refs

    def test_url_with_comment_ignores_words(self):
        c = classify_message("great read https://example.com/post")
        assert c.urls == ["https://example.com/post"]
        assert c.text is None and not c.unknown

    def test_long_text_captured(self):
        text = "Based rollups only make sense if L1 inclusion is cheap and fast."
        assert classify_message(text).text == text

    def test_short_text_unknown(self):
        c = classify_message("hmm ok")
        assert c.unknown == ["hmm ok"] and c.text is None

    def test_short_forwarded_text_captured(self):
        assert classify_message("short fwd", forwarded=True).text == "short fwd"

    def test_many_words_not_treated_as_handles(self):
        text = "this is a sentence with several plain words in it"
        c = classify_message(text)
        assert c.text == text and not c.usernames


class TestItems:
    def test_text_item_note(self):
        item = text_item("A thought about preconfs that is long enough.")
        assert item["item_id"].startswith("note_")
        assert item["content_type"] == "note"
        assert item["source"] == "telegram"
        assert item["url"] is None

    def test_text_item_forwarded_with_url(self):
        origin = {"name": "Some Channel", "username": "somechan", "url": "https://t.me/somechan/42"}
        item = text_item("Forwarded post body", origin)
        assert item["item_id"].startswith("blog_")
        assert item["url"] == "https://t.me/somechan/42"
        assert item["author_username"] == "somechan"
        assert item["content_type"] == "post"

    def test_text_item_deterministic(self):
        assert text_item("same text here ok")["item_id"] == text_item("same text here ok")["item_id"]

    def test_minimal_url_item(self):
        item = minimal_url_item("https://www.example.org/x")
        assert item["author_username"] == "example.org"
        assert item["text"] == "https://www.example.org/x"
        assert item["raw_data"]["unfetched"] is True


class TestForwardOrigin:
    def test_none(self):
        assert forward_origin(SimpleNamespace(forward_origin=None)) is None

    def test_channel(self):
        origin = SimpleNamespace(
            chat=SimpleNamespace(title="Chan", username="chan"), message_id=7, date=None,
        )
        info = forward_origin(SimpleNamespace(forward_origin=origin))
        assert info == {"name": "Chan", "username": "chan", "url": "https://t.me/chan/7"}

    def test_user(self):
        origin = SimpleNamespace(chat=None, sender_user=SimpleNamespace(full_name="A B", username="ab"), date=None)
        info = forward_origin(SimpleNamespace(forward_origin=origin))
        assert info["name"] == "A B" and info["username"] == "ab"

    def test_hidden_user(self):
        origin = SimpleNamespace(chat=None, sender_user=None, sender_user_name="Hidden", date=None)
        assert forward_origin(SimpleNamespace(forward_origin=origin))["name"] == "Hidden"
