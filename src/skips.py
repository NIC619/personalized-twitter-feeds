"""Silence as a weak negative: which delivered items count as skipped.

Upvotes dominate explicit votes (~90%), so the scorer rarely sees examples
of on-topic items the reader did not care about. An item that was sent,
never voted on and never opened is such an example. This module decides
which of those become ``skip`` signals; the scheduler records them.

Two sources write ``skip``:

* ``ignored``: automatic, a few days after delivery, only for digest days
  the reader engaged with (voted on or opened at least one item that day),
  so a day the digest was never read adds nothing. Exploration items are
  left out: nobody is expected to answer every 🎲.
* ``meh``: the reader tapped "rest are meh" under a digest. Deliberate, so
  it covers every unvoted item of that digest, exploration included.

A later real vote on the item wins (latest vote per item everywhere).
"""

from datetime import datetime, timezone
from typing import Iterable, Optional


def _sent_day(item: dict) -> Optional[str]:
    raw = item.get("sent_to_telegram")
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw.astimezone(timezone.utc).date().isoformat()
    return str(raw)[:10]


def pick_skips(
    sent_items: Iterable[dict],
    engaged_ids: set[str],
    require_engaged_day: bool = True,
    include_exploration: bool = False,
) -> list[dict]:
    """Delivered items that should get a ``skip`` signal.

    Args:
        sent_items: rows with item_id, sent_to_telegram, exploration,
            telegram_message_id
        engaged_ids: ids that carry a vote (any value) or an open
        require_engaged_day: only mark items from days the reader engaged
            with (by the UTC date of ``sent_to_telegram``)
        include_exploration: also mark 🎲 items

    Returns:
        The rows to mark, in input order.
    """
    items = [i for i in sent_items if i.get("item_id") and _sent_day(i)]
    engaged_days = {_sent_day(i) for i in items if i["item_id"] in engaged_ids}
    out = []
    for item in items:
        if item["item_id"] in engaged_ids:
            continue
        if item.get("exploration") and not include_exploration:
            continue
        if require_engaged_day and _sent_day(item) not in engaged_days:
            continue
        out.append(item)
    return out


def skip_signal(item: dict, source: str) -> dict:
    """Signal row for :meth:`DatabaseClient.save_signals`."""
    return {
        "item_id": item["item_id"],
        "kind": "vote",
        "value": "skip",
        "strength": 1,
        "reason": None,
        "source": source,
        "telegram_message_id": item.get("telegram_message_id"),
    }
