"""Configuration management using pydantic-settings."""

from pydantic_settings import BaseSettings
from pydantic import Field
from typing import Optional


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Twitter API v2 Credentials
    twitter_api_key: str = Field(..., description="Twitter API key")
    twitter_api_secret: str = Field(..., description="Twitter API secret")
    twitter_access_token: str = Field(..., description="Twitter access token")
    twitter_access_secret: str = Field(..., description="Twitter access token secret")
    twitter_bearer_token: str = Field(..., description="Twitter bearer token")

    # Anthropic API
    anthropic_api_key: str = Field(..., description="Anthropic API key")

    # Telegram Bot
    telegram_bot_token: str = Field(..., description="Telegram bot token")
    telegram_chat_id: str = Field(..., description="Telegram chat ID")

    # Supabase
    supabase_url: str = Field(..., description="Supabase project URL")
    supabase_key: str = Field(..., description="Supabase anon key")

    # Configuration
    fetch_hours: int = Field(default=24, description="Hours to look back for tweets")
    max_tweets: int = Field(default=100, description="Maximum tweets to fetch")
    filter_threshold: int = Field(default=50, description="Default threshold: items scoring at or above this are sent (calibrated per model with scripts/eval_prompt.py)")
    favorite_threshold_offset: int = Field(default=20, description="How much lower the threshold is for starred authors")
    muted_threshold_offset: int = Field(default=15, description="How much higher the threshold is for muted authors")
    starred_author_max_tweets: int = Field(default=10, description="Max tweets to fetch per starred author's timeline")
    schedule_hour: int = Field(default=9, description="Hour to run daily curation")
    schedule_timezone: str = Field(default="Asia/Taipei", description="Timezone for scheduling")
    schedule_skip_weekdays: str = Field(
        default="",
        description="Comma-separated weekday names to skip (e.g. 'sunday' or "
                    "'saturday,sunday'). Case-insensitive.",
    )

    # Exploration: random below-threshold items sent each run (marked 🎲)
    # so recall below the threshold can be measured from votes.
    exploration_per_run: int = Field(
        default=2,
        description="Number of random below-threshold items to send per run for recall measurement",
    )

    # RSS/Atom feeds polled in the daily run
    feed_max_entries_per_run: int = Field(
        default=10, description="Newest entries fetched per feed per run"
    )

    # Newsletters by email: unseen messages in this IMAP folder are read in
    # the daily run (and on /inbox), their links extracted and scored.
    email_imap_host: Optional[str] = Field(default=None, description="IMAP host, e.g. imap.gmail.com (unset disables)")
    email_imap_user: Optional[str] = Field(default=None, description="Mailbox login")
    email_imap_password: Optional[str] = Field(default=None, description="Mailbox password / app password")
    email_imap_folder: str = Field(default="INBOX", description="Folder (Gmail label) to read")
    email_max_per_run: int = Field(default=10, description="Max emails processed per run")
    email_delete_processed: bool = Field(default=False, description="Delete emails after processing instead of marking them read")

    # Preference profiles compiled from signals
    profile_compile_weekday: str = Field(
        default="sunday",
        description="Weekday to recompile preference profiles in scheduled mode (empty disables)",
    )
    profile_source: str = Field(
        default="compiled",
        description="'compiled' injects the latest compiled profiles into scoring "
                    "(falls back to the static baseline when none exist); 'static' always uses the baseline",
    )

    # Environment / deployment
    development_mode: bool = Field(
        default=False,
        description="When true, run with Telegram long-polling (local dev). "
                    "When false (default, production), run with webhooks.",
    )
    webhook_url: Optional[str] = Field(
        default=None,
        description="Public HTTPS base URL for the Telegram webhook, e.g. "
                    "https://app.up.railway.app. Required when development_mode=false.",
    )
    redirect_base_url: Optional[str] = Field(
        default=None,
        description="Public base URL for click-tracking links (/r/<item_id>). "
                    "Defaults to WEBHOOK_URL in production; links stay direct in dev.",
    )
    port: int = Field(
        default=8080,
        description="Port the webhook server binds to. Railway injects this via the PORT env var.",
    )

    # OpenAI Embeddings (Phase 2 RAG)
    rag_enabled: bool = Field(default=True, description="Enable RAG context in Claude prompts (requires OpenAI key)")
    openai_api_key: Optional[str] = Field(default=None, description="OpenAI API key for embeddings")
    embedding_model: str = Field(default="text-embedding-3-small", description="OpenAI embedding model")
    rag_similarity_limit: int = Field(default=3, description="Nearest voted neighbours attached to each item for scoring")
    claude_model: str = Field(default="claude-sonnet-5-5", description="Claude model used for scoring")
    claude_effort: str = Field(default="medium", description="output_config.effort for scoring calls (low/medium/high)")

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


def get_settings() -> Settings:
    """Get application settings."""
    return Settings()
