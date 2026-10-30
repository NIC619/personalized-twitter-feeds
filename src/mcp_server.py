#!/usr/bin/env python3
"""MCP server exposing the preference store to any agent.

Run over stdio:

    python -m src.mcp_server

Claude Code:

    claude mcp add preferences -- /path/to/.venv/bin/python -m src.mcp_server

(from the repo directory, or set `cwd`). Reads credentials from .env like
the bot does. See docs/mcp.md.
"""

import logging
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as _Server  # noqa: E402
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # noqa: E402

from config.settings import get_settings  # noqa: E402
from src.claude_filter import DOMAINS, ClaudeFilter  # noqa: E402
from src.database import DatabaseClient  # noqa: E402
from src.embeddings import EmbeddingManager  # noqa: E402
from src.preference_api import PreferenceAPI  # noqa: E402

logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

mcp = _Server(
    "preferences",
    instructions=(
        "Personal preference store: what this reader likes and skips, built "
        "from their votes on tweets, articles and anything they sent in. "
        "Use get_profile for a compact description, search_liked for "
        "concrete examples near a topic, score_items to predict interest in "
        "new content, and record_signal to teach it something new."
    ),
)

_api: Optional[PreferenceAPI] = None


def api() -> PreferenceAPI:
    global _api
    if _api is None:
        settings = get_settings()
        db = DatabaseClient(url=settings.supabase_url, key=settings.supabase_key)
        emb = EmbeddingManager(
            api_key=settings.openai_api_key, model=settings.embedding_model, db_client=db,
        ) if settings.rag_enabled else None
        claude = ClaudeFilter(api_key=settings.anthropic_api_key, model=settings.claude_model) \
            if settings.anthropic_api_key else None
        _api = PreferenceAPI(db, emb, claude)
    return _api


@mcp.tool()
def get_profile(domain: Optional[str] = None) -> str:
    """The reader's compiled preference profile as Markdown.

    Args:
        domain: One of the topic domains (ethereum-protocol, crypto, software,
            ai, science, economics, politics, culture, life, other) or omit
            for the full document with an overall summary.
    """
    return api().get_profile(domain)


@mcp.tool()
def search_liked(query: str, domain: Optional[str] = None, limit: int = 10) -> list[dict]:
    """Items the reader voted on that are most similar to a query.

    Each result carries the vote (up/down), strength (2 = strong like), the
    reader's own reason when they gave one, and a similarity score.

    Args:
        query: Topic or description to search near.
        domain: Optional domain filter.
        limit: Max results (1-50).
    """
    return api().search_liked(query, domain=domain, limit=limit)


@mcp.tool()
def score_items(items: list[dict]) -> list[dict]:
    """Predict how much the reader wants each item (0-100) with a reason and domain.

    Args:
        items: List of {title?, text?, url?, author?}. Nothing is stored.
    """
    return api().score_items(items)


@mcp.tool()
def record_signal(
    vote: Optional[str] = None,
    url: Optional[str] = None,
    item_id: Optional[str] = None,
    text: Optional[str] = None,
    title: Optional[str] = None,
    strength: int = 1,
    reason: Optional[str] = None,
    note: Optional[str] = None,
    domain: Optional[str] = None,
    source: str = "agent",
) -> dict:
    """Teach the store: record a vote and/or a note on an item, creating it if new.

    Args:
        vote: 'up' or 'down' (omit to only attach a note).
        url: Item URL (looked up, created if unknown).
        item_id: Existing item id (alternative to url).
        text: Content text for a new item without a URL (e.g. a quote, an idea).
        title: Optional title for a new item.
        strength: 2 for a strong like, else 1.
        reason: Why the reader voted this way, in their words.
        note: Free-text note to attach.
        domain: Optional domain tag for a new item.
        source: Who is writing (e.g. 'claude-code', 'browser').
    """
    return api().record_signal(
        vote=vote, url=url, item_id=item_id, text=text, title=title,
        strength=strength, reason=reason, note=note, domain=domain, source=source,
    )


@mcp.tool()
def list_domains() -> list[str]:
    """Topic domains used to tag items and compile profiles."""
    return list(DOMAINS)


if __name__ == "__main__":
    mcp.run()
