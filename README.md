# Content Curator

AI-powered content curation that filters your Twitter timeline and blog posts using Claude and delivers relevant content to Telegram.

## Features

- **Smart Filtering**: Claude AI scores tweets and blog posts based on your interests (blockchain research, Ethereum scaling, smart contract security)
- **Telegram Delivery**: Filtered content sent to your Telegram with engagement metrics
- **Blog Post Support**: Like individual blog posts or parse entire newsletters to score and review all articles
- **Feedback Loop**: Thumbs up/down buttons to improve future curation via RAG
- **Persistent Storage**: All tweets, blog posts, and feedback stored in Supabase

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
2. Go to SQL Editor and run the schema:
  ```bash
   python scripts/setup_database.py
  ```
   Copy the printed SQL and paste into Supabase SQL Editor.
   If you've previously run this script, re-run it now to add the `error_log` table introduced for persistent error tracking. All DDL is idempotent (`IF NOT EXISTS`); wrap any `CREATE POLICY "anon_all"` lines in `DROP POLICY IF EXISTS ...;` if the policy was already created.
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
| `/like url` | Upvote a tweet or blog post with a reason category. Accepts tweet URLs, blog post URLs, or numeric tweet IDs. |
| `/newsletter url` | Parse a newsletter, score all articles with Claude, and send them for review. On first use of a domain, prompts to select sections to ignore. |
| `/newsletter_prefs domain_or_url` | Edit ignored sections for a newsletter. Accepts a domain name or a full URL to re-extract sections. |
| `/thread tweet_url` | Fetch a full thread and display it as a single compiled message. Give it the last tweet in the thread. |
| `/starred` | List all currently starred authors. |
| `/blockword` | Block keyword(s) from the pipeline. Paste one per line or comma-separated. Whole-word, case-insensitive. Starred authors are exempt. |
| `/blockwords` | List blocked keywords; tap a keyword to remove it. |
| `/stats` | Show author performance stats (paginated). |
| `/help` | Show help message with available commands. |

Inline buttons on delivered content allow thumbs up/down voting with reason categories, starring/muting authors, and undoing recent votes.

**Shortcut**: you can omit the command and just send a message — tweet URLs, blog URLs, and numeric tweet IDs are treated as `/like`, while profile URLs, `@mentions`, and plain usernames are treated as `/star`. Mixed tokens in one message are dispatched accordingly.

### Count Timeline Tweets

Check how many tweets are in your Twitter timeline (fetches directly from Twitter):

```bash
# Last 24 hours
python scripts/count_twitter_timeline.py

# Last 48 hours, fetch up to 300 tweets
python scripts/count_twitter_timeline.py --hours 48 --max 300
```

Use this to check if `MAX_TWEETS` is high enough to capture all your timeline activity.

### A/B Test Prompts

Compare different Claude filter prompts to find which best predicts your preferences. See [AB_TESTING_PLAN.md](AB_TESTING_PLAN.md) for full details.

```bash
# 1. Enable in .env:
#    AB_TEST_ENABLED=true
#    AB_TEST_EXPERIMENT_ID=exp_001
#    AB_TEST_CHALLENGER_PROMPT=V3

# 2. Run normally — shadow scoring happens automatically
python main.py --once

# 3. After voting on ~30-50 tweets, generate report
python main.py --ab-report exp_001
```

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
python main.py --ab-report exp_001 # A/B test report for an experiment
python main.py --ab-report exp_001 --threshold 80  # Custom threshold for precision/recall
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
│   ├── claude_filter.py    # Claude AI filtering
│   ├── telegram_bot.py     # Telegram bot with feedback buttons
│   ├── database.py         # Supabase operations
│   ├── embeddings.py       # RAG embeddings for feedback-driven curation
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
│   ├── ab_test_report.py          # A/B test analysis report
│   ├── error_report.py            # Monthly error log report
│   ├── test_components.py         # Integration testing (live APIs)
│   └── count_twitter_timeline.py  # Count tweets from Twitter
├── main.py                 # CLI entry point
├── requirements.txt
├── requirements-dev.txt    # Test dependencies
└── .env.example
```

## How It Works

### Tweets
1. **Fetch**: Pulls tweets from your home timeline (last 24 hours)
2. **Pre-filter**: Items containing blocked keywords (managed via `/blockword`) are dropped before Claude scoring. Matching is whole-word, case-insensitive, across tweet text, X Article title/body, and quoted-tweet text. Content from starred authors is currently exempt from the blocklist.
3. **Filter**: Claude scores each tweet 0-100 based on your interests
4. **Send**: Tweets scoring ≥70 are sent to Telegram with 👍/👎 buttons
5. **Store**: All tweets and feedback saved to Supabase
6. **Learn**: Past feedback improves future filtering via RAG

### Blog Posts
1. **Like**: Send `/like <blog_url>` to fetch, score, and save a blog post you enjoyed
2. **Newsletter**: Send `/newsletter <url>` to parse a newsletter — all articles are scored and sent for review (no filtering threshold, all are shown)
3. **Section Filtering**: On first use of a newsletter domain, you're prompted to select sections to ignore (e.g. Regulation, Sponsor). Preferences are saved per domain. Edit later with `/newsletter_prefs`.
4. **Feedback**: Vote on blog posts just like tweets to train the system
5. **Store**: Blog posts stored alongside tweets with `content_type='blog_post'`

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

### Feedback & RAG

- **RAG context is built only from explicitly voted tweets** (top-5 similar) — unvoted tweets don't influence future scoring. Trusts explicit signal only; ignores the implicit "sent but never reacted to" signal.




| Variable            | Default     | Description                             |
| ------------------- | ----------- | --------------------------------------- |
| `FETCH_HOURS`       | 24          | Hours to look back for tweets           |
| `MAX_TWEETS`        | 100         | Maximum tweets to fetch per run         |
| `FILTER_THRESHOLD`  | 70          | Minimum score to send to Telegram       |
| `SCHEDULE_HOUR`     | 9           | Hour to run daily curation (24h format) |
| `SCHEDULE_TIMEZONE` | Asia/Taipei | Timezone for scheduling                 |
| `SCHEDULE_SKIP_WEEKDAYS` | (empty) | Comma-separated weekday names to skip in scheduled mode (e.g. `sunday`). Evaluated in `SCHEDULE_TIMEZONE`. |
| `RAG_ENABLED`       | true        | Enable RAG context in Claude prompts    |
| `AB_TEST_ENABLED`   | false       | Enable A/B testing of filter prompts    |
| `AB_TEST_EXPERIMENT_ID` | -       | Experiment ID for current A/B test      |
| `AB_TEST_CHALLENGER_PROMPT` | V1  | Prompt registry key for challenger      |


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

- Phase 1: Core MVP (fetch, filter, send, feedback buttons)
- Phase 2: RAG with embeddings (use feedback to improve filtering)
- Phase 3: Blog post and newsletter support
- Phase 4: LLM-based blog post filtering (once enough feedback is accumulated)
- Phase 5: RSS feed support, analytics dashboard, multi-account support

## License

MIT