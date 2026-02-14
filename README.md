# Twitter Curator

AI-powered Twitter feed curation that filters your timeline using Claude and delivers relevant tweets to Telegram.

## Features

- **Smart Filtering**: Claude AI scores tweets based on your interests (blockchain research, Ethereum scaling, smart contract security)
- **Telegram Delivery**: Filtered tweets sent to your Telegram with engagement metrics
- **Feedback Loop**: Thumbs up/down buttons to improve future curation (Phase 2: RAG)
- **Persistent Storage**: All tweets and feedback stored in Supabase

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
   Copy the printed SQL and paste into Supabase SQL Editor
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

### Bot Only Mode

Run just the Telegram bot (for receiving feedback on already-sent tweets):

```bash
python main.py --bot-only
```

### Count Timeline Tweets

Check how many tweets are in your Twitter timeline (fetches directly from Twitter):

```bash
# Last 24 hours
python scripts/count_twitter_timeline.py

# Last 48 hours, fetch up to 300 tweets
python scripts/count_twitter_timeline.py --hours 48 --max 300
```

Use this to check if `MAX_TWEETS` is high enough to capture all your timeline activity.

### CLI Options

```bash
python main.py --once              # Run once with default settings
python main.py --once -n 20        # Fetch only 20 tweets
python main.py --once --hours 48   # Look back 48 hours instead of 24
python main.py --schedule          # Run daily at configured hour
python main.py --bot-only          # Run Telegram bot only
python main.py --test              # Test all components
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
│   ├── embeddings.py       # Phase 2: RAG embeddings (stub)
│   └── scheduler.py        # Daily curation orchestration
├── tests/
│   ├── conftest.py                # Shared test fixtures
│   ├── test_claude_filter.py      # ClaudeFilter unit tests
│   ├── test_database.py           # DatabaseClient unit tests
│   ├── test_twitter_client.py     # TwitterClient unit tests
│   ├── test_scheduler.py          # DailyCurator unit tests
│   ├── test_telegram_bot.py       # TelegramCurator unit tests
│   └── test_embeddings.py         # EmbeddingManager unit tests
├── scripts/
│   ├── setup_database.py          # Database schema SQL
│   ├── test_components.py         # Integration testing (live APIs)
│   └── count_twitter_timeline.py  # Count tweets from Twitter
├── main.py                 # CLI entry point
├── requirements.txt
├── requirements-dev.txt    # Test dependencies
└── .env.example
```

## How It Works

1. **Fetch**: Pulls tweets from your home timeline (last 24 hours)
2. **Filter**: Claude scores each tweet 0-100 based on your interests
3. **Send**: Tweets scoring ≥70 are sent to Telegram with 👍/👎 buttons
4. **Store**: All tweets and feedback saved to Supabase
5. **Learn**: (Phase 2) Past feedback improves future filtering via RAG

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

## Configuration Options


| Variable            | Default     | Description                             |
| ------------------- | ----------- | --------------------------------------- |
| `FETCH_HOURS`       | 24          | Hours to look back for tweets           |
| `MAX_TWEETS`        | 100         | Maximum tweets to fetch per run         |
| `FILTER_THRESHOLD`  | 70          | Minimum score to send to Telegram       |
| `SCHEDULE_HOUR`     | 9           | Hour to run daily curation (24h format) |
| `SCHEDULE_TIMEZONE` | Asia/Taipei | Timezone for scheduling                 |


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

Logs are written to `curator.log` and stdout. Check here for errors and curation stats.

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
- Phase 3: Analytics dashboard, multi-account support

## License

MIT