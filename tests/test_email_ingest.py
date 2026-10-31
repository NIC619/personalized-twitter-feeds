"""Tests for newsletter email parsing and the IMAP mailbox reader."""

from email.message import EmailMessage
from unittest.mock import MagicMock

from src.email_ingest import MailboxFetcher, parse_email


def _multipart(subject="ETH Daily #1", sender="ETH Daily <hello@ethdaily.io>"):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["Date"] = "Fri, 31 Oct 2026 08:00:00 +0000"
    msg.set_content("Plain version: Fusaka ships https://blog.ethereum.org/fusaka")
    msg.add_alternative(
        "<html><head><style>p{}</style></head><body>"
        "<p>Top story: <a href='https://substack.com/redirect/abc?j=1'>Fusaka ships</a></p>"
        "<p><a href='mailto:x@y'>mail</a> <a href='https://ethdaily.io/unsub'></a></p>"
        "<script>track()</script></body></html>",
        subtype="html",
    )
    return msg.as_bytes()


class TestParseEmail:
    def test_html_body_with_inline_links(self):
        mail = parse_email(_multipart())
        assert mail["subject"] == "ETH Daily #1"
        assert mail["sender"] == "hello@ethdaily.io" and mail["sender_name"] == "ETH Daily"
        assert mail["date"].startswith("2026-10-31T08:00:00")
        assert "Fusaka ships <https://substack.com/redirect/abc?j=1>" in mail["text"]
        assert "track()" not in mail["text"] and "mailto" not in mail["text"]

    def test_plain_only(self):
        msg = EmailMessage()
        msg["Subject"] = "Plain"
        msg["From"] = "a@b.c"
        msg.set_content("just text https://example.com/x")
        mail = parse_email(msg.as_bytes())
        assert mail["text"].strip() == "just text https://example.com/x"
        assert mail["html"] is None

    def test_missing_headers(self):
        mail = parse_email(b"\r\nbody only")
        assert mail["subject"] == "(no subject)" and mail["sender"] is None


class _FakeImap:
    def __init__(self, messages):
        self.messages = messages  # uid(bytes) → raw bytes
        self.flags: dict[bytes, str] = {}
        self.selected = None

    def login(self, user, password):
        return "OK", []

    def select(self, folder):
        self.selected = folder
        return ("OK", [b"1"]) if folder in ("INBOX", "Newsletters", '"My Letters"') else ("NO", [])

    def uid(self, cmd, *args):
        if cmd == "search":
            unseen = [u for u in self.messages if self.flags.get(u) != "seen"]
            return "OK", [b" ".join(unseen)]
        if cmd == "fetch":
            uid = args[0]
            return "OK", [(b"1 (BODY[] {n}", self.messages[uid]), b")"]
        if cmd == "store":
            self.flags[args[0] if isinstance(args[0], bytes) else args[0].encode()] = "seen"
            return "OK", []
        raise AssertionError(cmd)

    def close(self):
        pass

    def logout(self):
        pass


class TestMailboxFetcher:
    def test_fetch_unseen_and_mark_seen(self):
        imap = _FakeImap({b"7": _multipart("Issue 7"), b"8": _multipart("Issue 8")})
        f = MailboxFetcher("imap.x", "u", "p", folder="Newsletters", imap_factory=lambda: imap)

        mails = f.fetch_unseen(limit=10)
        assert [m["subject"] for m in mails] == ["Issue 7", "Issue 8"]
        assert mails[0]["uid"] == "7"
        assert imap.selected == "Newsletters"

        f.mark_seen("7")
        assert [m["subject"] for m in f.fetch_unseen()] == ["Issue 8"]

    def test_limit(self):
        imap = _FakeImap({b"1": _multipart("a"), b"2": _multipart("b")})
        f = MailboxFetcher("imap.x", "u", "p", imap_factory=lambda: imap)
        assert len(f.fetch_unseen(limit=1)) == 1

    def test_folder_with_space_is_quoted(self):
        imap = _FakeImap({})
        f = MailboxFetcher("imap.x", "u", "p", folder="My Letters", imap_factory=lambda: imap)
        assert f.fetch_unseen() == []
        assert imap.selected == '"My Letters"'

    def test_missing_folder_raises(self):
        import pytest
        f = MailboxFetcher("imap.x", "u", "p", folder="Nope", imap_factory=lambda: _FakeImap({}))
        with pytest.raises(RuntimeError):
            f.fetch_unseen()
