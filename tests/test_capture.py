"""Tests for message classification and item construction."""

from types import SimpleNamespace

from src.capture import classify_lines, classify_message, forward_origin, minimal_url_item, text_item


class TestClassifyLines:
    def test_one_item_per_line_with_own_reason(self):
        groups = classify_lines(
            "https://x.com/a/status/1 great read about wallet privacy\n"
            "https://x.com/b/status/2 formal verification"
        )
        assert [(g.tweet_refs, g.comment) for g in groups] == [
            (["https://x.com/a/status/1"], "great read about wallet privacy"),
            (["https://x.com/b/status/2"], "formal verification"),
        ]

    def test_reason_on_following_line(self):
        groups = classify_lines("https://a.io/p\nwhy I liked it\nhttps://b.io/q")
        assert [(g.urls, g.comment) for g in groups] == [
            (["https://a.io/p"], "why I liked it"), (["https://b.io/q"], None),
        ]

    def test_leading_text_belongs_to_first_url(self):
        groups = classify_lines("I like\nhttps://a.io/p\nhttps://b.io/q boring")
        assert [(g.urls, g.comment) for g in groups] == [
            (["https://a.io/p"], "I like"), (["https://b.io/q"], "boring"),
        ]

    def test_verb_lines_are_their_own_groups(self):
        groups = classify_lines(":like https://a.io/p\n:dislike https://b.io/q shallow\n:star @x")
        assert [g.action for g in groups] == ["like", "dislike", "star"]
        assert groups[1].comment == "shallow"

    def test_multiline_note_stays_one_note(self):
        groups = classify_lines(":note first line\nsecond line https://a.io/p")
        assert len(groups) == 1 and groups[0].action == "note"
        assert groups[0].text == "first line\nsecond line https://a.io/p"

    def test_single_line_and_plain_text_unchanged(self):
        assert classify_lines("https://a.io/p ok")[0].comment == "ok"
        plain = classify_lines("two lines\nno urls at all")
        assert len(plain) == 1 and plain[0].unknown
        assert classify_lines("") == []

    def test_forwarded_multiline_without_url_is_one_note(self):
        groups = classify_lines("line one\nline two", forwarded=True)
        assert len(groups) == 1 and groups[0].action == "note"


class TestClassifyMessage:
    def test_tweet_url_is_like(self):
        c = classify_message("https://x.com/a/status/123")
        assert c.action == "like" and c.vote == "up"
        assert c.tweet_refs == ["https://x.com/a/status/123"]
        assert not c.urls and not c.usernames and c.text is None and c.comment is None

    def test_url_with_words_keeps_words_as_reason(self):
        c = classify_message("I like https://example.com/post it's a great read")
        assert c.action == "like" and c.urls == ["https://example.com/post"]
        assert c.comment == "I like it's a great read"
        assert not c.unknown

    def test_profile_url_stars(self):
        c = classify_message("https://twitter.com/vitalikbuterin")
        assert c.action == "star" and c.usernames == ["https://twitter.com/vitalikbuterin"]

    def test_bare_handle_is_not_a_command(self):
        c = classify_message("@dankrad")
        assert c.action is None and c.unknown == ["@dankrad"] and not c.usernames
        assert classify_message("dankrad").unknown == ["dankrad"]

    def test_star_verb(self):
        c = classify_message(":star @dankrad vitalikbuterin https://x.com/bob")
        assert c.action == "star"
        assert c.usernames == ["@dankrad", "vitalikbuterin", "https://x.com/bob"]

    def test_star_verb_without_target(self):
        assert classify_message(":star").unknown == [":star"]

    def test_like_verb_with_reason(self):
        c = classify_message(":like https://x.com/a/status/1 sharp take on blobs")
        assert c.action == "like" and c.tweet_refs == ["https://x.com/a/status/1"]
        assert c.comment == "sharp take on blobs"

    def test_like_verb_numeric_id(self):
        c = classify_message(":like 1234567890123")
        assert c.tweet_refs == ["1234567890123"] and c.comment is None

    def test_bare_numeric_ids_are_not_commands(self):
        assert classify_message("123 456").unknown == ["123 456"]

    def test_dislike_verb_and_alias(self):
        c = classify_message(":dislike https://example.com/x shallow")
        assert c.action == "dislike" and c.vote == "down" and c.comment == "shallow"
        assert classify_message(":down https://example.com/x").vote == "down"

    def test_like_verb_without_target(self):
        c = classify_message(":like just words")
        assert c.unknown and not c.urls

    def test_verb_is_case_insensitive(self):
        assert classify_message(":Like https://example.com/x").action == "like"

    def test_note_verb(self):
        c = classify_message(":note Based rollups only make sense if L1 inclusion is cheap.")
        assert c.action == "note" and c.text == "Based rollups only make sense if L1 inclusion is cheap."

    def test_note_verb_empty(self):
        assert classify_message(":note").unknown == [":note"]

    def test_plain_text_is_not_captured(self):
        text = "Based rollups only make sense if L1 inclusion is cheap and fast."
        c = classify_message(text)
        assert c.action is None and c.text is None and c.unknown == [text]

    def test_short_text_unknown(self):
        c = classify_message("hmm ok")
        assert c.unknown == ["hmm ok"] and c.text is None

    def test_forwarded_text_captured(self):
        c = classify_message("short fwd", forwarded=True)
        assert c.action == "note" and c.text == "short fwd"

    def test_forwarded_with_url_likes_url(self):
        c = classify_message("look https://example.com/p", forwarded=True)
        assert c.action == "like" and c.urls == ["https://example.com/p"] and c.text is None


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
