# X SDK (xdk) Migration Notes

Surveyed: 2026-02-07
**Migrated: 2026-04-12**

## Status

**Migration complete.** `tweepy` has been replaced with `xdk` (v0.9.0) in `src/twitter_client.py`. All 178 tests pass.

## Package Info

- **Package**: `xdk>=0.9.0`
- **Python**: 3.8+ (CPython and PyPy)
- **Docs**: https://docs.x.com/xdks/python/overview
- **Examples**: https://github.com/xdevplatform/samples/tree/main/python
- **Generated from**: Official OpenAPI spec at https://api.x.com/2/openapi.json

## What Changed

### `src/twitter_client.py`

| Area | Before (tweepy) | After (xdk) |
|------|-----------------|-------------|
| Client init | `tweepy.Client(bearer_token=..., consumer_key=..., ...)` | `xdk.Client(bearer_token=..., auth=OAuth1(...))` |
| Home timeline | `client.get_home_timeline()` | `client.users.get_timeline(id=user_id)` |
| User lookup | `client.get_user(username=...)` | `client.users.get_by_username(username=...)` |
| User tweets | `client.get_users_tweets(id=...)` | `client.users.get_posts(id=...)` |
| Single tweet | `client.get_tweet(id=...)` | `client.posts.get_by_id(id=...)` |
| Errors | `tweepy.TweepyException` | `requests.exceptions.HTTPError` |
| Pagination | Manual `next_token` loop | Auto-pagination generator |
| Response data | Attribute access (`tweet.text`) | Dict access (`tweet["text"]`) |

### Key implementation notes

- `get_timeline` requires user ID, so `__init__` calls `get_me()` to resolve it
- XDK paginated methods return generators that auto-follow `next_token`
- Response `data` items are plain dicts, not typed objects
- `_extract_article` simplified to accept a single tweet dict (no separate `tweet_data`/`tweet_obj`)

### `tests/test_twitter_client.py`

- Mocks changed from `SimpleNamespace` (attribute access) to dicts
- Fixture patches `xdk.Client` and `OAuth1` instead of `tweepy.Client`
- All `fetch_timeline` tests use `max_results=5`

### `requirements.txt`

- `tweepy>=4.14.0` → `xdk>=0.9.0`

## Environment Variables (unchanged)

```bash
TWITTER_API_KEY=...       # Consumer Key / API Key
TWITTER_API_SECRET=...    # Consumer Secret / API Secret
TWITTER_ACCESS_TOKEN=...
TWITTER_ACCESS_SECRET=...
TWITTER_BEARER_TOKEN=...
```

## Resources

- X SDK Docs: https://docs.x.com/xdks/python/overview
- X SDK Auth: https://docs.x.com/xdks/python/authentication
- GitHub Samples: https://github.com/xdevplatform/samples/tree/main/python
