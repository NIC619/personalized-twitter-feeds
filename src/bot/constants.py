"""Shared constants for the Telegram bot modules."""

# Seconds a vote can be undone before it is saved
VOTE_GRACE_SECONDS = 10

# Telegram's sendMessage text limit (parsed characters; we budget on raw HTML,
# which is longer, so staying under this is conservative)
TELEGRAM_MESSAGE_LIMIT = 4096

# Conversation states
STAR_AWAITING_INPUT = 0
LIKE_AWAITING_INPUT = 1
THREAD_AWAITING_INPUT = 2
NEWSLETTER_AWAITING_INPUT = 3
BLOCKWORD_AWAITING_INPUT = 5
