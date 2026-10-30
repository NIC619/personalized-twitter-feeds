"""Pure helpers for the feedback flow: reactions, vote buttons, reason prompts.

Kept free of Telegram I/O so they are trivially testable; TelegramCurator
wires them to handlers.
"""

from dataclasses import dataclass
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# Reaction emoji → (vote, strength). Anything else is ignored.
REACTION_VOTES: dict[str, tuple[str, int]] = {
    "🔥": ("up", 2),
    "❤": ("up", 1),
    "❤️": ("up", 1),
    "👍": ("up", 1),
    "👏": ("up", 1),
    "💯": ("up", 2),
    "👎": ("down", 1),
    "💩": ("down", 1),
    "🤮": ("down", 1),
}

STRONG_LABEL = "🔥"
UP_LABEL = "👍"
DOWN_LABEL = "👎"


@dataclass(frozen=True)
class Vote:
    vote: str          # 'up' | 'down'
    strength: int = 1  # 1 normal, 2 strong


def vote_from_reactions(new_reaction, old_reaction) -> tuple[Optional[Vote], bool]:
    """Interpret a MessageReactionUpdated change.

    Args:
        new_reaction / old_reaction: sequences of ReactionType objects
            (only ReactionTypeEmoji, with an ``emoji`` attribute, count).

    Returns:
        (vote, retracted): ``vote`` is the vote now expressed by the new
        reaction set (strongest wins), or None. ``retracted`` is True when
        the old set expressed a vote and the new set expresses none.
    """
    def _best(reactions) -> Optional[Vote]:
        best: Optional[Vote] = None
        for r in reactions or []:
            emoji = getattr(r, "emoji", None)
            if emoji is None:
                continue
            mapped = REACTION_VOTES.get(emoji)
            if not mapped:
                continue
            candidate = Vote(*mapped)
            if best is None or candidate.strength > best.strength:
                best = candidate
        return best

    new = _best(new_reaction)
    old = _best(old_reaction)
    return new, (old is not None and new is None)


def vote_label(vote: Vote) -> str:
    if vote.vote == "down":
        return DOWN_LABEL
    return STRONG_LABEL if vote.strength >= 2 else UP_LABEL


def vote_buttons(
    item_id: str,
    username: Optional[str],
    fav_label: str = "⭐ Author",
    mute_label: str = "🔇 Mute",
) -> InlineKeyboardMarkup:
    """Default keyboard under a sent item: 🔥 👍 👎 + author controls."""
    rows = [[
        InlineKeyboardButton(STRONG_LABEL, callback_data=f"vote:{item_id}:up:2"),
        InlineKeyboardButton(UP_LABEL, callback_data=f"vote:{item_id}:up:1"),
        InlineKeyboardButton(DOWN_LABEL, callback_data=f"vote:{item_id}:down:1"),
    ]]
    if username:
        rows.append([
            InlineKeyboardButton(fav_label, callback_data=f"fav:{username}:{item_id}"),
            InlineKeyboardButton(mute_label, callback_data=f"mute:{username}:{item_id}"),
        ])
    return InlineKeyboardMarkup(rows)


def voted_buttons(item_id: str, vote: Vote, undo: bool) -> InlineKeyboardMarkup:
    """Keyboard shown after a vote: the vote label, plus Undo during the grace period."""
    row = [InlineKeyboardButton(vote_label(vote), callback_data="voted")]
    if undo:
        row.append(InlineKeyboardButton("↩ Undo", callback_data=f"undo:{item_id}"))
    return InlineKeyboardMarkup([row])


def parse_vote_callback(data: str) -> Optional[tuple[str, Vote]]:
    """Parse 'vote:{item_id}:{up|down}[:{strength}]' → (item_id, Vote)."""
    parts = data.split(":")
    if len(parts) not in (3, 4) or parts[0] != "vote":
        return None
    vote = parts[2]
    if vote not in ("up", "down"):
        return None
    strength = 1
    if len(parts) == 4:
        try:
            strength = max(1, min(2, int(parts[3])))
        except ValueError:
            return None
    if vote == "down":
        strength = 1
    return parts[1], Vote(vote, strength)


def reason_prompt_text(vote: Vote, proposal: str) -> str:
    verb = "Skipped" if vote.vote == "down" else ("Loved" if vote.strength >= 2 else "Liked")
    return f"{vote_label(vote)} {verb} because: <i>{proposal}</i>?"


def reason_prompt_buttons(item_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅", callback_data=f"rsn_ok:{item_id}"),
        InlineKeyboardButton("✏️ Edit", callback_data=f"rsn_edit:{item_id}"),
        InlineKeyboardButton("✖ Skip", callback_data=f"rsn_skip:{item_id}"),
    ]])


def reason_done_text(vote: Vote, reason: str) -> str:
    return f"{vote_label(vote)} ✅ <i>{reason}</i>"
