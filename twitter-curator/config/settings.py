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
    filter_threshold: int = Field(default=70, description="Minimum score to send")
    schedule_hour: int = Field(default=9, description="Hour to run daily curation")
    schedule_timezone: str = Field(default="Asia/Taipei", description="Timezone for scheduling")

    # Optional: Embedding service (Phase 2)
    voyage_api_key: Optional[str] = Field(default=None, description="Voyage AI API key")

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


def get_settings() -> Settings:
    """Get application settings."""
    return Settings()
