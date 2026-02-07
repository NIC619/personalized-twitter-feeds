# X SDK (xdk) Migration Notes

Surveyed: 2026-02-07

## Overview

X (Twitter) released an official Python SDK called `xdk`. This document captures research for potential future migration from `tweepy`.

## Package Info

- **Package**: `xdk>=0.4.5`
- **Python**: 3.8+ (CPython and PyPy)
- **Docs**: https://docs.x.com/xdks/python/overview
- **Examples**: https://github.com/xdevplatform/samples/tree/main/python
- **Generated from**: Official OpenAPI spec at https://api.x.com/2/openapi.json

## Key Features

- Supports all X API v2 endpoints (search, timelines, filtered-stream, etc.)
- **Automatic pagination** (no manual `next_token` handling)
- Type hints and modern Python patterns
- Three auth methods: Bearer Token, OAuth 2.0 PKCE, OAuth 1.0a

## Authentication

### OAuth 1.0a with Pre-existing Tokens (Recommended for our use case)

No interactive login required - use existing access tokens:

```python
from xdk import Client
from xdk.oauth1_auth import OAuth1

oauth1 = OAuth1(
    api_key="YOUR_API_KEY",           # Consumer Key
    api_secret="YOUR_API_SECRET",     # Consumer Secret
    access_token="YOUR_ACCESS_TOKEN",
    access_token_secret="YOUR_ACCESS_TOKEN_SECRET",
    callback="http://localhost:8080/callback"  # Required but not used
)

client = Client(auth=oauth1)
```

### Bearer Token (App-only, read-only)

```python
from xdk import Client

client = Client(bearer_token="YOUR_BEARER_TOKEN")
```

### OAuth 2.0 PKCE (Interactive)

Requires user to visit URL and paste callback - not suitable for automated scripts.

## API Examples

### Get Authenticated User

```python
response = client.users.get_me(
    user_fields=["created_at", "description"]
)
user = response.data
user_id = user["id"]
```

### Get Home Timeline

```python
response = client.users.get_timeline(
    id=user_id,
    tweet_fields=["created_at", "text", "public_metrics", "entities", "author_id"],
    expansions=["author_id"],
    user_fields=["username", "name"],
    max_results=100
)

# Auto-pagination example
for page in client.users.get_timeline(id=user_id, max_results=100):
    tweets = page.data
    # process tweets
```

### Get User by Username

```python
response = client.users.get_users_by_usernames(
    usernames=["elonmusk", "Twitter"],
    user_fields=["created_at", "description"]
)
```

## Comparison: xdk vs tweepy

| Feature | xdk | tweepy |
|---------|-----|--------|
| Maintainer | Official X SDK | Community |
| Version | 0.4.5 (new) | 4.14.0 (mature) |
| OAuth 1.0a | ✅ | ✅ |
| Auto-pagination | ✅ Built-in | ⚠️ Manual |
| Rate limit handling | ❓ Unknown | ✅ `wait_on_rate_limit` |
| Community/docs | Limited | Extensive |
| Stability | New, may have bugs | Battle-tested |

## Migration Effort

If migrating `src/twitter_client.py`:

1. Replace `tweepy` import with `xdk`
2. Change client initialization to use `OAuth1` class
3. Update `fetch_timeline()` to use `client.users.get_timeline()`
4. Simplify pagination (auto-handled)
5. Adjust response parsing (slightly different structure)

Estimated effort: ~1-2 hours

## Recommendation

**Wait for xdk v1.0+** before migrating:
- Current tweepy implementation works fine
- xdk is very new, less documentation and community support
- Risk of undiscovered bugs in production
- Monitor xdk releases for stability improvements

## Environment Variables (same as current)

```bash
TWITTER_API_KEY=...      # Consumer Key / API Key
TWITTER_API_SECRET=...   # Consumer Secret / API Secret
TWITTER_ACCESS_TOKEN=...
TWITTER_ACCESS_SECRET=...
TWITTER_BEARER_TOKEN=...  # Optional for app-only requests
```

## Resources

- X SDK Docs: https://docs.x.com/xdks/python/overview
- X SDK Auth: https://docs.x.com/xdks/python/authentication
- GitHub Samples: https://github.com/xdevplatform/samples/tree/main/python
- Home Timeline Example: https://github.com/xdevplatform/samples/blob/main/python/users/timeline/get_home_timeline.py
