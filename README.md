# Preference Curator

A personal preference store with a Twitter curator on top. Claude filters your
timeline and delivers the good parts to Telegram; everything you vote on, react
to, send in, or import becomes a signal. Those signals are compiled into a
preference profile that drives the filter, and that any agent can read over MCP.

## Features

- **Smart filtering**: Claude scores tweets 0-100 against your compiled preference profile, seeing each item's nearest past votes. Strict tool-use output, no JSON parsing.
- **Telegram delivery** with 🔥/👍/👎 buttons, emoji reactions as votes, model-proposed reasons you confirm in one tap, reply-to-note, and click tracking.
- **Capture anything**: send the bot a URL, a forwarded post, or a paragraph of text and it is stored as a liked item in the same store.
- **Imports**: Raindrop, Readwise, generic CSV, or your Twitter archive's likes.
- **Compiled profiles**: a weekly job turns votes, reasons and notes into per-domain Markdown profiles (`PREFERENCES.md`), versioned in the database.
- **Agent access**: an MCP server exposes `get_profile`, `search_liked`, `score_items`, `record_signal`.
- **Measurable**: offline eval against all votes, plus daily exploration items so recall below the threshold is measured too.

## Prerequisites

- Python 3.11+
- Twitter API v2 access (Basic tier - $100/month)
- Anthropic API key
- Telegram account
- Supabase account (free tier)

## Setup

### 1. Clone and Install

```bash
cd twitter-curator
pip install -r requirements.txt
cp .env.example .env
```

### 2. Twitter API Credentials

1. Go to [Twitter Developer Portal](https://developer.twitter.com/)
2. Create or select your app
3. Navigate to "Keys and Tokens"
4. Get these credentials:


| .env Variable           | Where to Find                                        |
| ----------------------- | ---------------------------------------------------- |
| `TWITTER_API_KEY`       | Consumer Key (OAuth 1.0 Keys)                        |
| `TWITTER_API_SECRET`    | Consumer Secret (click Regenerate, save immediately) |
| `TWITTER_ACCESS_TOKEN`  | Access Token (click Generate)                        |
| `TWITTER_ACCESS_SECRET` | Access Token Secret (shown with Access Token)        |
| `TWITTER_BEARER_TOKEN`  | Bearer Token (App-Only Authentication)               |


> **Note**: Secrets are only shown once during generation. Save them immediately.

### 3. Anthropic API Key

1. Go to [Anthropic Console](https://console.anthropic.com/)
2. Create an API key
3. Add to `.env` as `ANTHROPIC_API_KEY`

### 4. Telegram Bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot` and follow the prompts
3. Save the bot token as `TELEGRAM_BOT_TOKEN`
4. Get your Chat ID:
  - Start a chat with your new bot (send any message)
  - Open in browser: `https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates`
  - Find `"chat":{"id":123456789}` - that number is your `TELEGRAM_CHAT_ID`

### 5. Supabase Database

1. Go to [Supabase](https://supabase.com/) and create a project
2. Go to SQL Editor and run the schema.
   - **Fresh install**: `python scripts/setup_database.py` prints the full schema; paste it into the SQL Editor.
   - **Existing deployment**: run the files in `scripts/migrations/` in date order instead. They add
     `items`, `signals`, `item_embeddings`, `preference_profiles` and copy your `tweets`,
     `feedback` and `tweet_embeddings` rows into them. The legacy tables are left in place; drop
     them yourself once you are happy. All migrations are idempotent.
3. Get your credentials from Settings > API:
  - `SUPABASE_URL`: Project URL
  - `SUPABASE_KEY`: anon/public key

### 6. Configure .env

Fill in all values in your `.env` file:

```bash
# Twitter API
TWITTER_API_KEY=your_consumer_key
TWITTER_API_SECRET=your_consumer_secret
TWITTER_ACCESS_TOKEN=your_access_token
TWITTER_ACCESS_SECRET=your_access_secret
TWITTER_BEARER_TOKEN=your_bearer_token

# Anthropic
ANTHROPIC_API_KEY=your_api_key

# Telegram
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id

# Supabase
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=your_anon_key

# Configuration (optional - defaults shown)
FETCH_HOURS=24
MAX_TWEETS=100
FILTER_THRESHOLD=70
SCHEDULE_HOUR=9
SCHEDULE_TIMEZONE=Asia/Taipei
SCHEDULE_SKIP_WEEKDAYS=          # e.g. "sunday" or "saturday,sunday"
```

## Usage

### Run Tests

Run the unit test suite (no API credentials needed):

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

### Test Components

Verify all API connections work (requires live credentials):

```bash
python scripts/test_components.py
```

### Run Once

Fetch, filter, and send tweets one time:

```bash
python main.py --once
```

### Run Scheduled

Run daily curation at configured hour with Telegram bot for feedback:

```bash
python main.py --schedule
```

By default the scheduler waits for the next configured tick before running curation — restarts (deploys, crashes) don't re-send a digest. To run one curation immediately on startup (useful during development or a first deploy), add `--run-once-on-startup`:

```bash
python main.py --schedule --run-once-on-startup
```

### Bot Only Mode

Run just the Telegram bot (for receiving feedback on already-sent tweets):

```bash
python main.py --bot-only
```

### Telegram Bot Commands

| Command | Description |
| --- | --- |
| `/star username` | Toggle starred status for an author. Accepts username, @mention, or profile/tweet URL. |
| `/like url` | Upvote a tweet, article or any URL. Accepts tweet URLs, other URLs, or numeric tweet IDs. |
| `/newsletter url` | Parse a newsletter, score all articles with Claude, and send them for review. |
| `/newsletter_prefs domain_or_url` | Edit ignored sections for a newsletter. |
| `/thread tweet_url` | Fetch a full thread and display it as a single compiled message. |
| `/starred` | List all currently starred authors. |
| `/blockword` | Block keyword(s) from the pipeline. Whole-word, case-insensitive. Starred authors are exempt. |
| `/blockwords` | List blocked keywords; tap a keyword to remove it. |
| `/stats` | Show author performance stats (paginated). |
| `/help` | Show help message with available commands. |

**You rarely need commands.** Anything you send is captured:

| You send | What happens |
| --- | --- |
| A tweet URL or numeric id | Fetched, scored, stored, upvoted |
| Any other URL | Page fetched, scored, stored, upvoted (falls back to a bare-URL item if the page can't be fetched) |
| A forwarded post (channel, user) | Stored as an item with the origin, upvoted |
| A paragraph of text (20+ chars) | Stored as a note item, upvoted |
| `@handle` or a profile URL | Author starred |
| A reply to a delivered item | Saved as a note on that item |

After every upvote the bot proposes a reason and you confirm or edit it.

### Importing what you already liked

```bash
python scripts/import_items.py raindrop raindrop.csv
python scripts/import_items.py readwise reader.csv
python scripts/import_items.py csv likes.csv --name pocket         # any CSV with a url column
python scripts/import_items.py twitter-archive data/like.js --username you
python scripts/import_items.py raindrop raindrop.csv --dry-run     # parse only
```

Each row becomes an item plus an upvote (`source = import:<name>`) and an embedding when
`OPENAI_API_KEY` is set (otherwise run `scripts/backfill_embeddings.py` later).
Add `--tag-domains` to have Claude assign domains during import.

### Compiled preference profiles

Votes, reasons and notes are compiled per domain (`ethereum-protocol`, `crypto`, `software`,
`ai`, `science`, `economics`, `politics`, `culture`, `life`, `other`) into Markdown profiles.
The scoring prompt injects the latest profiles (`PROFILE_SOURCE=compiled`); the static baseline
in `src/claude_filter.py` is only the fallback.

```bash
python main.py --compile-profile                    # recompile now (also runs weekly in --schedule mode)
python main.py --export-preferences PREFERENCES.md  # write the latest profiles as one Markdown file
```

### Agent access (MCP)

```bash
claude mcp add preferences -- "$PWD/.venv/bin/python" -m src.mcp_server
```

Tools: `get_profile`, `search_liked`, `score_items`, `record_signal`, `list_domains`.
See [docs/mcp.md](docs/mcp.md).

### Count Timeline Tweets

Check how many tweets are in your Twitter timeline (fetches directly from Twitter):

```bash
# Last 24 hours
python scripts/count_twitter_timeline.py

# Last 48 hours, fetch up to 300 tweets
python scripts/count_twitter_timeline.py --hours 48 --max 300
```

Use this to check if `MAX_TWEETS` is high enough to capture all your timeline activity.

### Offline Prompt Evaluation

Every vote you cast is a label. `scripts/eval_prompt.py` re-scores all voted items with
the current prompt and reports how well the scores separate 👍 from 👎, so a prompt,
profile or model change can be judged in minutes instead of waiting months for live votes.
This replaced the shadow A/B framework (history kept in [AB_TESTING_PLAN.md](AB_TESTING_PLAN.md)).

```bash
python scripts/eval_prompt.py --label baseline                      # production model + neighbours
python scripts/eval_prompt.py --label no-rag --no-rag               # isolate the RAG effect
python scripts/eval_prompt.py --label sonnet55 --model claude-sonnet-5-5
python scripts/eval_prompt.py --label profile-v2 --profile-file my_profile.md
python scripts/eval_prompt.py --compare baseline no-rag sonnet55    # side-by-side report
```

Runs are saved under `eval_runs/` and the report always includes a `stored@send` column
(the scores production gave the items when they were sent). The headline metric is AUC.
Read the caveats at the top of the script: labels only exist for items that were sent,
so recall is over previously-sent upvotes. The 🎲 exploration items (see below) are the
only unbiased sample of what the filter misses.

### Monthly Error Report

Review WARNING/ERROR log records persisted by the pipeline:

```bash
python main.py --error-report 2026-03   # specific month
python main.py --error-report last      # previous calendar month
```

The report prints totals by level, a daily sparkline, top sources / error types / repeated messages, and the 10 most recent records.

### CLI Options

```bash
python main.py --once              # Run once with default settings
python main.py --once -n 20        # Fetch only 20 tweets
python main.py --once --hours 48   # Look back 48 hours instead of 24
python main.py --schedule          # Run daily at configured hour (waits for next tick)
python main.py --schedule --run-once-on-startup  # Also run one curation immediately
python main.py --bot-only          # Run Telegram bot only
python main.py --test              # Test all components
python main.py --error-report 2026-03  # Monthly error log report
python main.py --error-report last     # Error log report for previous month
```

## Project Structure

```
twitter-curator/
├── config/
│   └── settings.py         # Environment config with validation
├── src/
│   ├── twitter_client.py   # Twitter API v2 client
│   ├── capture.py                 # Classify sent messages, build items from URLs/text
│   ├── claude_filter.py    # Claude AI filtering
│   ├── telegram_bot.py     # Telegram bot with feedback buttons
│   ├── database.py         # Supabase operations
│   ├── embeddings.py
│   ├── feedback_flow.py           # Reactions, vote buttons, reason prompts (pure helpers)
│   ├── mcp_server.py              # MCP server over the preference store
│   ├── preference_api.py          # Agent-facing API (used by MCP)
│   ├── profile_compiler.py        # Signals → per-domain Markdown profiles
│   ├── webhook_server.py          # Telegram webhook + /r/<id> click tracking       # RAG embeddings for feedback-driven curation
│   ├── scheduler.py        # Daily curation orchestration
│   ├── content.py          # Content ID utilities (blog ID generation, URL detection)
│   ├── keyword_filter.py   # Pre-LLM blocklist filter (whole-word, case-insensitive)
│   ├── error_logger.py     # DatabaseErrorHandler: persists WARNING+ logs to Supabase
│   └── blog_fetcher.py     # Blog post fetching and newsletter parsing
├── tests/
│   ├── conftest.py                # Shared test fixtures
│   ├── test_claude_filter.py      # ClaudeFilter unit tests
│   ├── test_database.py           # DatabaseClient unit tests
│   ├── test_twitter_client.py     # TwitterClient unit tests
│   ├── test_scheduler.py          # DailyCurator unit tests
│   ├── test_telegram_bot.py       # TelegramCurator unit tests
│   ├── test_embeddings.py         # EmbeddingManager unit tests
│   ├── test_keyword_filter.py     # Keyword blocklist filter unit tests
│   ├── test_error_logger.py       # Error logger + monthly report unit tests
│   └── test_blog_fetcher.py       # BlogFetcher and content utils unit tests
├── scripts/
│   ├── setup_database.py          # Database schema SQL
│   ├── eval_prompt.py             # Offline prompt/model evaluation against votes
│   ├── import_items.py            # Import Raindrop/Readwise/CSV/Twitter-archive likes
│   ├── migrations/                # Incremental SQL to run in Supabase (existing deployments)
│   ├── error_report.py            # Monthly error log report
│   ├── test_components.py         # Integration testing (live APIs)
│   └── count_twitter_timeline.py  # Count tweets from Twitter
├── main.py                 # CLI entry point
├── requirements.txt
├── requirements-dev.txt    # Test dependencies
└── .env.example
```

## How It Works

### Data model

| Table | Holds |
| --- | --- |
| `items` | Anything from any source: tweets, articles, forwarded posts, notes, imports. Carries the LLM-assigned `domain`, score, and delivery state. |
| `signals` | Every preference event: `vote` (up/down, strength 1 or 2, reason), `note`, `open`. With `source` (button, reaction, like_cmd, reply, redirect, telegram, import:x, agent). |
| `item_embeddings` | pgvector embeddings for voted items, used for per-item neighbours and `search_liked`. |
| `preference_profiles` | Versioned compiled Markdown profiles per domain plus `overall`. |

### Daily run
1. **Fetch**: Pulls tweets from your home timeline (last 24 hours) plus starred authors' timelines
2. **Pre-filter**: Retweets from non-starred authors and blocked keywords are dropped before scoring
3. **Neighbours**: Each tweet is embedded and its 3 most similar past votes are looked up in pgvector
4. **Score**: Claude scores each tweet 0-100 against the compiled profile, seeing each tweet's own past-vote neighbours, and tags its domain
5. **Send**: Tweets at or above the threshold (per-author tiers apply) go to Telegram, plus 2 random below-threshold items marked 🎲 (exploration)
6. **Store**: Every scored item is saved; every vote, reaction, reason, note and link open becomes a signal
7. **Learn**: Signals feed neighbours immediately and the compiled profile weekly; the eval script measures the effect

### Captured content
`/like`, plain URLs, forwarded posts and free text go through the same scorer (threshold 0, so
they always get a score and a domain), are saved as items, and receive an upvote with an undo
window and a model-proposed reason.

## Filtering Criteria

Claude filters based on these interests:

**High Priority (90-100)**:

- Based rollups, preconfirmations
- TEEs, ZK proofs
- Ethereum scaling research

**Should Read (70-89)**:

- Technical content, audits
- Protocol analysis
- Smart contract security

**Skip (<70)**:

- Price speculation
- NFT drops
- Celebrity opinions
- Engagement farming

## Author's Design Preferences

The choices below reflect how I personally want this tool to behave. They are not technical requirements — if you fork this repo, expect to revisit them.

### Author tiers & filtering

- **Retweets from non-favorite authors are dropped entirely** (`src/scheduler.py`). Favorites' retweets are treated as curation signal worth reading; everyone else's are treated as noise.
- **Three author tiers with different score thresholds**: favorites pass at 50, defaults at 70, muted at 85 (`FILTER_THRESHOLD` ± offsets). Claude scores everything against the lowest floor so feedback signal isn't lost for muted authors.
- **Starred authors have a dedicated fetch path** alongside the home timeline, capped per author. Favorites are treated as a distinct content source, not just surfaced by timeline algorithms.
- **Keyword blocklist runs before Claude scoring; favorites are exempt.** Saves API cost on obvious noise, but trusts favorites' judgment even when they post about blocked topics.

### What counts as relevant

- **The active Claude prompt encodes a specific worldview** (`src/claude_filter.py`) — skewed toward protocol/infrastructure research, skeptical of price talk, NFTs, engagement farming, and celebrity drama. See the "Filtering Criteria" section above. Forking users will almost certainly want to rewrite the prompt.
- **The prompt biases toward false negatives** — most tweets should be skipped. If you'd rather over-include and prune, lower the threshold or pick a less strict prompt variant.

### Delivery & cadence

- **Single daily digest at 09:00 Asia/Taipei** (`SCHEDULE_HOUR`, `SCHEDULE_TIMEZONE`). Assumes you want to read Twitter once a day, not receive a continuous stream.
- **24-hour fetch window, max 100 tweets per run.** Trades recency against catch-up after absences.
- **Tweets grouped by thread (conversation_id) and sorted chronologically within threads** at delivery time. Optimizes for reading threads as a unit rather than flat chronological order.

### Feedback capture

- **Three ways to vote, lowest friction first**: react to the message with an emoji (🔥 strong like, 👍 / ❤️ like, 👎 skip; remove the reaction to retract), tap the 🔥 👍 👎 buttons, or `/like` a URL. Reactions save immediately; buttons keep a 10-second undo.
- **The model proposes the reason, you confirm it.** After every vote the bot replies with a one-line guess ("Liked because: concrete preconf economics?") and ✅ / ✏️ / skip. Free text beats fixed categories, and it is what the compiled profile is built from. The old eight reason buttons are gone; their labels survive as reason text on past votes.
- **Reply to any item to attach a note.**
- **Opens are tracked.** In production, item links go through `/r/<item_id>` on the bot's own web server, which records an `open` signal and redirects. Set `REDIRECT_BASE_URL` to override (defaults to `WEBHOOK_URL`); links stay direct in `DEVELOPMENT_MODE`.
- **Everything lands in the `signals` table** (`kind`: vote / note / open, with `strength`, `reason`, `source`). Votes are also still written to the legacy `feedback` table until phase 3 moves reads over.

### Feedback & RAG

- **Neighbours come only from explicitly voted items** (top-3 per item, `RAG_SIMILARITY_LIMIT`) — unvoted tweets don't influence future scoring. Trusts explicit signal only; ignores the implicit "sent but never reacted to" signal.
- **Exploration over pure exploitation**: `EXPLORATION_PER_RUN` (default 2) below-threshold items are sent each run, clearly marked 🎲. Votes on them are the only unbiased measurement of recall.




| Variable            | Default     | Description                             |
| ------------------- | ----------- | --------------------------------------- |
| `FETCH_HOURS`       | 24          | Hours to look back for tweets           |
| `MAX_TWEETS`        | 100         | Maximum tweets to fetch per run         |
| `FILTER_THRESHOLD`  | 70          | Minimum score to send to Telegram       |
| `SCHEDULE_HOUR`     | 9           | Hour to run daily curation (24h format) |
| `SCHEDULE_TIMEZONE` | Asia/Taipei | Timezone for scheduling                 |
| `SCHEDULE_SKIP_WEEKDAYS` | (empty) | Comma-separated weekday names to skip in scheduled mode (e.g. `sunday`). Evaluated in `SCHEDULE_TIMEZONE`. |
| `RAG_ENABLED`       | true        | Attach voted neighbours to items when scoring |
| `RAG_SIMILARITY_LIMIT` | 3        | Neighbours per item                     |
| `EXPLORATION_PER_RUN` | 2         | Random below-threshold items sent per run (🎲) |
| `CLAUDE_MODEL`      | claude-sonnet-4-6 | Scoring model                     |
| `REDIRECT_BASE_URL` | `WEBHOOK_URL` | Base URL for click-tracking links; unset in dev = direct links |


## Troubleshooting

### "Failed to load settings"

- Make sure `.env` file exists and has all required variables
- Check for typos in variable names

### Twitter API errors

- Verify all 5 Twitter credentials are correct
- Check your API tier supports home timeline access (Basic tier required)
- Consumer Secret and Access Token Secret are only shown once - regenerate if lost

### Telegram bot not responding

- Make sure you started a chat with your bot first
- Verify `TELEGRAM_CHAT_ID` is correct (should be a number)
- Check bot token is valid

### Database errors

- Run `python scripts/setup_database.py` and execute SQL in Supabase
- Verify `SUPABASE_URL` and `SUPABASE_KEY` are correct
- Check that tables exist in Supabase Table Editor

## Logs

Logs are written to `curator.log` and stdout for day-to-day curation stats. WARNING/ERROR records are additionally persisted to the Supabase `error_log` table via a custom logging handler attached alongside the console/file sinks, so trends can be reviewed later with `python main.py --error-report <YYYY-MM>`. DB writes are best-effort — if Supabase is down, the record falls back to stderr and the pipeline continues.

## Cost Estimates


| Service             | Monthly Cost |
| ------------------- | ------------ |
| Twitter API (Basic) | $100         |
| Claude API          | ~$5-10       |
| Telegram            | Free         |
| Supabase            | Free tier    |
| **Total**           | ~$105-110    |


## Roadmap

See [ROADMAP.md](ROADMAP.md). Next: RSS feeds and LLM-based newsletter extraction (phase 4).

## License

MIT