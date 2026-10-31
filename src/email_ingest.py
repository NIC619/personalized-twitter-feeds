"""Newsletters by email: read a mailbox over IMAP, turn each message into page text.

Forward (or auto-filter) newsletter emails into a mailbox or folder the bot
can read. Each unseen message becomes ``{uid, subject, sender, date, text}``
where ``text`` is the visible body with links inline as ``anchor <href>``,
ready for ``ClaudeFilter.extract_newsletter_entries``. Messages are marked
seen only after they have been processed, so a failed run retries them.

Stdlib only (``imaplib`` + ``email``) plus BeautifulSoup for the HTML body.
"""

import email
import email.policy
import imaplib
import logging
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime
from typing import Optional

from src.blog_fetcher import html_to_page_text

logger = logging.getLogger(__name__)


def parse_email(raw: bytes) -> dict:
    """Parse a raw RFC 822 message into {subject, sender, sender_name, date, text, html}."""
    msg: EmailMessage = email.message_from_bytes(raw, policy=email.policy.default)
    sender_name, sender = parseaddr(str(msg.get("From", "")))
    date = None
    if msg.get("Date"):
        try:
            date = parsedate_to_datetime(msg["Date"]).isoformat()
        except (TypeError, ValueError):
            date = None
    html_body = _part_text(msg, "html")
    plain_body = _part_text(msg, "plain")
    text = html_to_page_text(html_body) if html_body else (plain_body or "").strip()
    return {
        "subject": str(msg.get("Subject", "")).strip() or "(no subject)",
        "sender": sender or None,
        "sender_name": sender_name or None,
        "date": date,
        "text": text,
        "html": html_body,
    }


def _part_text(msg: EmailMessage, subtype: str) -> Optional[str]:
    """Decoded text of the first ``text/<subtype>`` part (or the body itself)."""
    if msg.is_multipart():
        part = msg.get_body(preferencelist=(subtype,))
    else:
        part = msg if msg.get_content_subtype() == subtype else None
    if part is None:
        return None
    try:
        return part.get_content()
    except (LookupError, UnicodeDecodeError):
        payload = part.get_payload(decode=True) or b""
        return payload.decode(part.get_content_charset() or "utf-8", errors="replace")


class MailboxFetcher:
    """Unseen messages from one IMAP folder."""

    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        folder: str = "INBOX",
        port: int = 993,
        imap_factory=None,
    ):
        self.host = host
        self.user = user
        self.password = password
        self.folder = folder
        self.port = port
        self._imap_factory = imap_factory or (lambda: imaplib.IMAP4_SSL(host, port))

    def _connect(self):
        conn = self._imap_factory()
        conn.login(self.user, self.password)
        status, _ = conn.select(self._quote(self.folder))
        if status != "OK":
            raise RuntimeError(f"IMAP folder not found: {self.folder}")
        return conn

    @staticmethod
    def _quote(folder: str) -> str:
        return f'"{folder}"' if " " in folder and not folder.startswith('"') else folder

    def fetch_unseen(self, limit: int = 10) -> list[dict]:
        """Oldest ``limit`` unseen messages, parsed. Does not mark them seen."""
        conn = self._connect()
        try:
            status, data = conn.uid("search", None, "UNSEEN")
            if status != "OK":
                raise RuntimeError("IMAP search failed")
            uids = (data[0] or b"").split()
            out = []
            for uid in uids[:limit]:
                status, parts = conn.uid("fetch", uid, "(BODY.PEEK[])")
                if status != "OK" or not parts:
                    logger.warning(f"IMAP fetch failed for uid {uid!r}")
                    continue
                raw = next((p[1] for p in parts if isinstance(p, tuple) and len(p) > 1), None)
                if raw is None:
                    continue
                parsed = parse_email(raw)
                parsed["uid"] = uid.decode() if isinstance(uid, bytes) else str(uid)
                out.append(parsed)
            if uids:
                logger.info(f"Mailbox {self.folder}: {len(uids)} unseen, processing {len(out)}")
            return out
        finally:
            self._close(conn)

    def mark_seen(self, uid: str) -> None:
        conn = self._connect()
        try:
            conn.uid("store", uid, "+FLAGS", "(\\Seen)")
        finally:
            self._close(conn)

    @staticmethod
    def _close(conn) -> None:
        for fn in (conn.close, conn.logout):
            try:
                fn()
            except Exception:
                pass
