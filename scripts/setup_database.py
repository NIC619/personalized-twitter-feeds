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

-- Table: muted_authors
-- Stores Twitter accounts the user wants stricter filtering for
CREATE TABLE IF NOT EXISTS muted_authors (
    id BIGSERIAL PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,  -- Twitter username (lowercase, without @)
    added_at TIMESTAMP DEFAULT NOW(),
    notes TEXT  -- Optional notes about why they're muted
);

-- Index for muted_authors
CREATE INDEX IF NOT EXISTS idx_muted_authors_username ON muted_authors(username);

-- Table: tweet_embeddings (Phase 2)
-- Stores vector embeddings for similarity search
CREATE TABLE IF NOT EXISTS tweet_embeddings (
    id BIGSERIAL PRIMARY KEY,
    tweet_id TEXT UNIQUE REFERENCES tweets(tweet_id) ON DELETE CASCADE,
    embedding vector(1536),  -- OpenAI text-embedding-3-small dimensions
    created_at TIMESTAMP DEFAULT NOW()
);

-- Index for vector similarity search (Phase 2)
-- Note: Adjust 'lists' parameter based on data size
-- Rule of thumb: lists = sqrt(row_count)
CREATE INDEX IF NOT EXISTS idx_tweet_embeddings ON tweet_embeddings
USING ivfflat (embedding vector_cosine_ops)
WITH (lists = 100);

-- Row Level Security: enable on all tables, allow anon role full access
ALTER TABLE public.tweets ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.feedback ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.favorite_authors ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.muted_authors ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.tweet_embeddings ENABLE ROW LEVEL SECURITY;

CREATE POLICY "anon_all" ON public.tweets FOR ALL TO anon USING (true) WITH CHECK (true);
CREATE POLICY "anon_all" ON public.feedback FOR ALL TO anon USING (true) WITH CHECK (true);
CREATE POLICY "anon_all" ON public.favorite_authors FOR ALL TO anon USING (true) WITH CHECK (true);
CREATE POLICY "anon_all" ON public.muted_authors FOR ALL TO anon USING (true) WITH CHECK (true);
CREATE POLICY "anon_all" ON public.tweet_embeddings FOR ALL TO anon USING (true) WITH CHECK (true);

-- Function: match_voted_tweets
-- Find similar tweets that have user feedback, using pgvector cosine distance
CREATE OR REPLACE FUNCTION match_voted_tweets(
    query_embedding vector(1536),
    match_count int DEFAULT 5
)
RETURNS TABLE (
    tweet_id text,
    text text,
    author_username text,
    vote text,
    similarity float
)
LANGUAGE plpgsql
AS $$
BEGIN
    RETURN QUERY
    SELECT
        t.tweet_id,
        t.text,
        t.author_username,
        f.user_vote AS vote,
        1 - (te.embedding <=> query_embedding) AS similarity
    FROM tweet_embeddings te
    JOIN tweets t ON t.tweet_id = te.tweet_id
    JOIN feedback f ON f.tweet_id = te.tweet_id
    ORDER BY te.embedding <=> query_embedding
    LIMIT match_count;
END;
$$;

-- Optional: Create a view for tweets with feedback
CREATE OR REPLACE VIEW tweets_with_feedback
WITH (security_invoker = true) AS
SELECT
    t.*,
    f.user_vote,
    f.voted_at,
    f.notes as feedback_notes
FROM tweets t
LEFT JOIN feedback f ON t.tweet_id = f.tweet_id;

-- Table: ab_test_scores
-- Stores A/B test scores for prompt comparison experiments
CREATE TABLE IF NOT EXISTS ab_test_scores (
    id BIGSERIAL PRIMARY KEY,
    tweet_id TEXT REFERENCES tweets(tweet_id) ON DELETE CASCADE,
    experiment_id TEXT NOT NULL,
    prompt_variant TEXT NOT NULL,  -- e.g. 'control', 'challenger'
    prompt_version TEXT NOT NULL,  -- e.g. 'V1', 'V2', 'V3'
    score FLOAT NOT NULL,
    reason TEXT,
    is_control BOOLEAN NOT NULL DEFAULT FALSE,
    scored_at TIMESTAMP DEFAULT NOW()
);

-- Indexes for ab_test_scores
CREATE INDEX IF NOT EXISTS idx_ab_test_experiment ON ab_test_scores(experiment_id);
CREATE INDEX IF NOT EXISTS idx_ab_test_tweet ON ab_test_scores(tweet_id);
CREATE INDEX IF NOT EXISTS idx_ab_test_variant ON ab_test_scores(prompt_variant);

-- RLS for ab_test_scores
ALTER TABLE public.ab_test_scores ENABLE ROW LEVEL SECURITY;
CREATE POLICY "anon_all" ON public.ab_test_scores FOR ALL TO anon USING (true) WITH CHECK (true);

-- Function: get_ab_test_analysis
-- Joins control + challenger scores with feedback for paired comparison
CREATE OR REPLACE FUNCTION get_ab_test_analysis(p_experiment_id text)
RETURNS TABLE (
    tweet_id text,
    control_score float,
    control_reason text,
    control_prompt text,
    challenger_score float,
    challenger_reason text,
    challenger_prompt text,
    user_vote text,
    tweet_text text,
    author_username text
)
LANGUAGE plpgsql
AS $$
BEGIN
    RETURN QUERY
    SELECT
        c.tweet_id,
        c.score AS control_score,
        c.reason AS control_reason,
        c.prompt_version AS control_prompt,
        ch.score AS challenger_score,
        ch.reason AS challenger_reason,
        ch.prompt_version AS challenger_prompt,
        f.user_vote,
        t.text AS tweet_text,
        t.author_username
    FROM ab_test_scores c
    JOIN ab_test_scores ch
        ON c.tweet_id = ch.tweet_id
        AND c.experiment_id = ch.experiment_id
        AND ch.is_control = false
    LEFT JOIN feedback f ON f.tweet_id = c.tweet_id
    LEFT JOIN tweets t ON t.tweet_id = c.tweet_id
    WHERE c.experiment_id = p_experiment_id
        AND c.is_control = true
    ORDER BY c.scored_at DESC;
END;
$$;

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
