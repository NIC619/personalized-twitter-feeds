#!/usr/bin/env python3
"""Database setup script for Twitter Curator.

Run this script to create the required tables in Supabase.
You can either:
1. Run the SQL directly in Supabase SQL Editor (recommended)
2. Use this script to print the SQL commands
"""

SCHEMA_SQL = """
-- Enable pgvector extension (for Phase 2 embeddings)
CREATE EXTENSION IF NOT EXISTS vector;

-- Table: tweets
-- Stores all fetched tweets with filter results
CREATE TABLE IF NOT EXISTS tweets (
    id BIGSERIAL PRIMARY KEY,
    tweet_id TEXT UNIQUE NOT NULL,
    author_username TEXT NOT NULL,
    author_name TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL,
    fetched_at TIMESTAMP DEFAULT NOW(),
    metrics JSONB,  -- likes, retweets, replies, views
    url TEXT NOT NULL,
    raw_data JSONB,  -- Full tweet object
    filtered BOOLEAN DEFAULT FALSE,
    filter_score FLOAT,  -- Claude's confidence score (0-100)
    filter_reason TEXT,  -- Why it was filtered in/out
    sent_to_telegram TIMESTAMP,  -- When sent to user
    telegram_message_id INTEGER  -- Telegram message ID for reference
);

-- Indexes for tweets table
CREATE INDEX IF NOT EXISTS idx_tweets_tweet_id ON tweets(tweet_id);
CREATE INDEX IF NOT EXISTS idx_tweets_created_at ON tweets(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tweets_filtered ON tweets(filtered);
CREATE INDEX IF NOT EXISTS idx_tweets_sent ON tweets(sent_to_telegram);

-- Table: feedback
-- Stores user feedback (thumbs up/down) on tweets
CREATE TABLE IF NOT EXISTS feedback (
    id BIGSERIAL PRIMARY KEY,
    tweet_id TEXT REFERENCES tweets(tweet_id) ON DELETE CASCADE,
    user_vote TEXT CHECK (user_vote IN ('up', 'down')),
    voted_at TIMESTAMP DEFAULT NOW(),
    telegram_message_id INTEGER,
    notes TEXT  -- Optional user comments
);

-- Indexes for feedback table
CREATE INDEX IF NOT EXISTS idx_feedback_tweet_id ON feedback(tweet_id);
CREATE INDEX IF NOT EXISTS idx_feedback_vote ON feedback(user_vote);
CREATE INDEX IF NOT EXISTS idx_feedback_voted_at ON feedback(voted_at DESC);

-- Table: favorite_authors
-- Stores Twitter accounts the user likes
CREATE TABLE IF NOT EXISTS favorite_authors (
    id BIGSERIAL PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,  -- Twitter username (lowercase, without @)
    added_at TIMESTAMP DEFAULT NOW(),
    notes TEXT  -- Optional notes about why they're favorited
);

-- Index for favorite_authors
CREATE INDEX IF NOT EXISTS idx_favorite_authors_username ON favorite_authors(username);

-- Table: tweet_embeddings (Phase 2)
-- Stores vector embeddings for similarity search
CREATE TABLE IF NOT EXISTS tweet_embeddings (
    id BIGSERIAL PRIMARY KEY,
    tweet_id TEXT REFERENCES tweets(tweet_id) ON DELETE CASCADE,
    embedding vector(1024),  -- Adjust dimension based on embedding model
    created_at TIMESTAMP DEFAULT NOW()
);

-- Index for vector similarity search (Phase 2)
-- Note: Adjust 'lists' parameter based on data size
-- Rule of thumb: lists = sqrt(row_count)
CREATE INDEX IF NOT EXISTS idx_tweet_embeddings ON tweet_embeddings
USING ivfflat (embedding vector_cosine_ops)
WITH (lists = 100);

-- Optional: Create a view for tweets with feedback
CREATE OR REPLACE VIEW tweets_with_feedback AS
SELECT
    t.*,
    f.user_vote,
    f.voted_at,
    f.notes as feedback_notes
FROM tweets t
LEFT JOIN feedback f ON t.tweet_id = f.tweet_id;

-- Optional: Function to get feedback statistics
CREATE OR REPLACE FUNCTION get_feedback_stats()
RETURNS TABLE (
    total_tweets BIGINT,
    filtered_tweets BIGINT,
    thumbs_up BIGINT,
    thumbs_down BIGINT,
    no_feedback BIGINT
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        COUNT(*)::BIGINT as total_tweets,
        COUNT(*) FILTER (WHERE filtered = true)::BIGINT as filtered_tweets,
        COUNT(*) FILTER (WHERE f.user_vote = 'up')::BIGINT as thumbs_up,
        COUNT(*) FILTER (WHERE f.user_vote = 'down')::BIGINT as thumbs_down,
        COUNT(*) FILTER (WHERE f.user_vote IS NULL AND t.filtered = true)::BIGINT as no_feedback
    FROM tweets t
    LEFT JOIN feedback f ON t.tweet_id = f.tweet_id;
END;
$$ LANGUAGE plpgsql;
"""


def print_schema():
    """Print the schema SQL."""
    print("=" * 60)
    print("Twitter Curator Database Schema")
    print("=" * 60)
    print()
    print("Copy and paste the following SQL into Supabase SQL Editor:")
    print()
    print("-" * 60)
    print(SCHEMA_SQL)
    print("-" * 60)
    print()
    print("After running the SQL:")
    print("1. Go to Supabase Dashboard > Table Editor")
    print("2. Verify tables exist: tweets, feedback, favorite_authors, tweet_embeddings")
    print("3. Check that pgvector extension is enabled")
    print()


def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Setup database schema for Twitter Curator"
    )
    parser.add_argument(
        "--sql-only",
        action="store_true",
        help="Print SQL only, no additional output",
    )

    args = parser.parse_args()

    if args.sql_only:
        print(SCHEMA_SQL)
    else:
        print_schema()


if __name__ == "__main__":
    main()
