# Preference store as an MCP server

`src/mcp_server.py` exposes the preference store to any MCP-capable agent
(Claude Code, Claude Desktop, Cursor, your own agents) over stdio. It reads
the same `.env` the bot uses.

## Tools

| Tool | What it does |
|------|--------------|
| `get_profile(domain?)` | Compiled preference profile as Markdown. One domain, or the full document with the overall summary. Falls back to the static baseline when nothing has been compiled yet. |
| `search_liked(query, domain?, limit)` | Items you voted on that are nearest to a query, each with vote, strength, your reason and similarity. Semantic when `OPENAI_API_KEY` is set, text search otherwise. |
| `score_items(items)` | Predict 0-100 interest, a reason and a domain for new items `{title?, text?, url?, author?}`. Nothing is stored. |
| `record_signal(vote?, url?, item_id?, text?, title?, strength, reason?, note?, domain?, source)` | Teach the store: upvote/downvote and/or attach a note. Creates the item when the URL or text is new, and embeds it. |
| `list_domains()` | The domain tags used across items and profiles. |

## Claude Code

From the repo directory:

```bash
claude mcp add preferences -- "$PWD/.venv/bin/python" -m src.mcp_server
```

Then, in any Claude Code session: "check my preferences before ranking these
papers", "record that I liked this article because of X", and so on.

## Claude Desktop / other clients

```json
{
  "mcpServers": {
    "preferences": {
      "command": "/path/to/personalized-twitter-feeds/.venv/bin/python",
      "args": ["-m", "src.mcp_server"],
      "cwd": "/path/to/personalized-twitter-feeds"
    }
  }
}
```

## Without MCP

Everything the server does is a method on `src.preference_api.PreferenceAPI`,
and `python main.py --export-preferences PREFERENCES.md` writes the compiled
profile as a plain Markdown file any agent can read.
