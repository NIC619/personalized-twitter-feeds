"""Skips: "seen it, not for me", recorded only on the reader's explicit action.

Upvotes dominate explicit votes (~90%), so the scorer rarely sees examples
of on-topic items the reader did not care about. A ``skip`` vote is that
example: weaker than ``down`` in the scoring prompt and the compiled
profile. It is never inferred from silence; the reader records it by:

* the 😐 button (or a 😐 / 🤷 reaction) on one item → ``source=button`` /
  ``reaction``, like any other vote;
* "😐 Rest are meh" under a run → every item sent in that run with no vote
  and no open gets ``source=meh`` (this module picks them).

A later real vote on the item wins (latest vote per item everywhere).
"""

from typing import Iterable


def pick_skips(sent_items: Iterable[dict], engaged_ids: set[str]) -> list[dict]:
    """Delivered items with no vote and no open, in input order.

    Args:
        sent_items: rows with item_id, telegram_message_id (and anything else)
        engaged_ids: ids that carry a vote (any value) or an open
    """
    return [i for i in sent_items if i.get("item_id") and i["item_id"] not in engaged_ids]


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
