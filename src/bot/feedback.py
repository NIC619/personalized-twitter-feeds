"""Vote flow: buttons, reactions, undo, model-proposed reasons, click-tracking resolver.

Mixin for :class:`src.telegram_bot.TelegramCurator`; it relies on the
attributes that class sets up in ``__init__``.
"""

import asyncio
import html
import logging
import re
from typing import Optional

import telegram.error
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

from src.bot.constants import (
    BLOCKWORD_AWAITING_INPUT,
    LIKE_AWAITING_INPUT,
    NEWSLETTER_AWAITING_INPUT,
    STAR_AWAITING_INPUT,
    THREAD_AWAITING_INPUT,
    VOTE_GRACE_SECONDS,
)
from src.capture import classify_message, forward_origin
from src.content import is_blog_content, is_http_url, is_tweet_url, is_twitter_profile_url
from src.feedback_flow import (
    Vote,
    parse_vote_callback,
    reason_done_text,
    reason_prompt_buttons,
    reason_prompt_text,
    vote_buttons,
    vote_from_reaction_set,
    vote_from_reactions,
    vote_label,
    voted_buttons,
)
from src.webhook_server import redirect_url

logger = logging.getLogger(__name__)


class FeedbackMixin:
    async def _handle_feedback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle feedback button press."""
        query = update.callback_query
        try:
            await query.answer()
        except (telegram.error.BadRequest, telegram.error.NetworkError):
            logger.warning(f"Callback query answer failed, processing anyway: {query.data}")

        data = query.data

        try:
            await self._dispatch_feedback(query, data)
        except (telegram.error.BadRequest, telegram.error.NetworkError) as e:
            logger.debug(f"Error handling callback {data}: {e}")

    async def _dispatch_feedback(
        self, query, data: str
    ) -> None:
        """Dispatch feedback callback by type. Caller catches BadRequest."""
        # Handle blocked-keyword removal: "bkrm:{index}"
        if data.startswith("bkrm:"):
            await self._handle_blockword_remove(query, data)
            return

        # Handle vote: "vote:{item_id}:{up|down}[:{strength}]"
        if data.startswith("vote:"):
            parsed = parse_vote_callback(data)
            if not parsed:
                return
            item_id, vote = parsed
            await self._record_vote(
                item_id, vote, query.message.message_id, source="button",
                grace=True, query=query,
            )

        # Reason prompt: "rsn_ok|rsn_edit|rsn_skip:{item_id}"
        elif data.startswith(("rsn_ok:", "rsn_edit:", "rsn_skip:")):
            action, _, item_id = data.partition(":")
            prompt = self._item_prompts.get(item_id)
            if action == "rsn_ok":
                if prompt:
                    await self._confirm_reason(item_id, prompt[1])
            elif action == "rsn_edit":
                self._awaiting_reason[str(query.message.chat_id)] = item_id
                try:
                    await query.edit_message_text(
                        "✏️ Reply to this message with your reason.", parse_mode="HTML",
                    )
                except Exception:
                    pass
            else:  # skip
                self._item_prompts.pop(item_id, None)
                self._reason_prompts.pop(query.message.message_id, None)
                vote = self._item_votes.get(item_id)
                try:
                    await query.edit_message_text(
                        f"{vote_label(vote) if vote else '✔'} (no reason)", parse_mode="HTML",
                    )
                except Exception:
                    pass

        # Handle undo: "undo:{item_id}"
        elif data.startswith("undo:"):
            _, _, item_id = data.partition(":")
            if item_id not in self._pending_feedback:
                logger.info(f"Undo too late: item={item_id}, already saved")
                return
            await self._cancel_vote(item_id, restore_keyboard=False)
            self._item_votes.pop(item_id, None)
            username = self._tweet_authors.get(item_id)
            await query.edit_message_reply_markup(
                reply_markup=self._make_tweet_buttons(item_id, username)
            )
            logger.info(f"Feedback undone: item={item_id}")

        # Handle favorite author: "fav:{username}:{item_id}"
        elif data.startswith("fav:"):
            parts = data.split(":")
            if len(parts) != 3:
                return

            _, username, item_id = parts

            state = None
            if self.favorite_author_callback:
                try:
                    state = await self.favorite_author_callback(username=username)
                    logger.info(f"Toggle favorite @{username} → {state}")
                except Exception as e:
                    logger.error(f"Error toggling favorite author: {e}")

            label = f"⭐ @{username}" if state == "favorited" else f"⭐ Author"
            await query.edit_message_reply_markup(
                reply_markup=self._make_tweet_buttons(item_id, username, fav_label=label)
            )

        # Handle mute author: "mute:{username}:{item_id}"
        elif data.startswith("mute:"):
            parts = data.split(":")
            if len(parts) != 3:
                return

            _, username, item_id = parts

            state = None
            if self.mute_author_callback:
                try:
                    state = await self.mute_author_callback(username=username)
                    logger.info(f"Toggle mute @{username} → {state}")
                except Exception as e:
                    logger.error(f"Error toggling mute author: {e}")

            label = f"🔇 @{username}" if state == "muted" else f"🔇 Mute"
            await query.edit_message_reply_markup(
                reply_markup=self._make_tweet_buttons(item_id, username, mute_label=label)
            )

    async def _item_for_message(self, message_id: int) -> Optional[str]:
        item_id = self._message_items.get(message_id)
        if item_id or not self.lookup_message_callback:
            return item_id
        try:
            item = await self.lookup_message_callback(message_id)
        except Exception as e:
            logger.warning(f"Message lookup failed for {message_id}: {e}")
            return None
        if item:
            self._message_items[message_id] = item["item_id"]
            self._tweet_authors.setdefault(item["item_id"], item.get("author_username"))
            return item["item_id"]
        return None

    async def _send_liked_item(
        self,
        item_id: str,
        message: str,
        vote: Optional[Vote] = None,
        reason: Optional[str] = None,
    ) -> bool:
        """Send a captured item and record an immediate vote (with undo)."""
        vote = vote or Vote("up", 1)
        try:
            sent = await self.application.bot.send_message(
                chat_id=self.chat_id,
                text=message,
                reply_markup=voted_buttons(item_id, vote, undo=True),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.error(f"Error sending liked item {item_id}: {e}")
            return False
        await self._remember_sent(item_id, sent.message_id)
        await self._record_vote(
            item_id, vote, sent.message_id, source="like_cmd", grace=True, reason=reason,
        )
        return True

    async def _remember_sent(self, item_id: str, message_id: int) -> None:
        """Map a sent message to its item, in memory and (if wired) in the DB
        so reactions and reply-notes still resolve after a restart."""
        self._message_items[message_id] = item_id
        if self.mark_sent_callback:
            try:
                await self.mark_sent_callback(item_id, message_id)
            except Exception as e:
                logger.warning(f"Could not persist message id for {item_id}: {e}")

    async def _record_vote(
        self,
        item_id: str,
        vote: Vote,
        message_id: int,
        source: str,
        grace: bool,
        query=None,
        reason: Optional[str] = None,
    ) -> None:
        """Register a vote: show it, save it (after an undo grace period for
        buttons), then ask the model to propose a reason unless ``reason``
        was given by the user."""
        await self._cancel_vote(item_id, restore_keyboard=False)
        self._item_votes[item_id] = vote
        self._message_items[message_id] = item_id
        generation = self._vote_generation.get(item_id, 0) + 1
        self._vote_generation[item_id] = generation

        await self._set_keyboard(
            message_id, voted_buttons(item_id, vote, undo=grace), query=query
        )
        if reason:
            # The user already said why: the save carries it, no proposal.
            self._pending_reasons[item_id] = reason

        async def _save(delay: float):
            if delay:
                await asyncio.sleep(delay)
            notes = self._pending_reasons.pop(item_id, None)
            if self.feedback_callback:
                try:
                    await self.feedback_callback(
                        item_id=item_id,
                        vote=vote.vote,
                        telegram_message_id=message_id,
                        notes=notes,
                        strength=vote.strength,
                        source=source,
                    )
                    logger.info(
                        f"Feedback recorded: item={item_id}, vote={vote.vote}, "
                        f"strength={vote.strength}, source={source}"
                    )
                except Exception as e:
                    logger.error(f"Error recording feedback: {e}")
            self._pending_feedback.pop(item_id, None)
            if grace:
                await self._set_keyboard(message_id, voted_buttons(item_id, vote, undo=False))

        if grace:
            task = asyncio.create_task(_save(VOTE_GRACE_SECONDS))
            self._pending_feedback[item_id] = {"task": task, "message_id": message_id}
        else:
            await _save(0)

        if reason:
            return
        # Proposals call the model (seconds). Run them off the update
        # handler so an Undo tapped meanwhile is processed in time.
        self._background(self._propose_reason(item_id, vote, message_id, generation))

    def _background(self, coro) -> asyncio.Task:
        """Run ``coro`` as a tracked background task."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _cancel_vote(self, item_id: str, restore_keyboard: bool = True) -> None:
        """Cancel a pending (unsaved) vote and clean up its reason prompt."""
        pending = self._pending_feedback.pop(item_id, None)
        if pending:
            pending["task"].cancel()
        self._pending_reasons.pop(item_id, None)
        await self._remove_reason_prompt(item_id)
        if pending and restore_keyboard:
            await self._set_keyboard(
                pending["message_id"],
                self._make_tweet_buttons(item_id, self._tweet_authors.get(item_id)),
            )

    async def _set_keyboard(self, message_id: int, markup, query=None) -> None:
        try:
            if query is not None:
                await query.edit_message_reply_markup(reply_markup=markup)
            else:
                await self.application.bot.edit_message_reply_markup(
                    chat_id=self.chat_id, message_id=message_id, reply_markup=markup,
                )
        except Exception as e:
            logger.debug(f"Keyboard update skipped for {message_id}: {e}")

    def _vote_is_current(self, item_id: str, generation: Optional[int]) -> bool:
        """False once the vote was undone, retracted or re-cast since ``generation``."""
        return (
            item_id in self._item_votes
            and (generation is None or self._vote_generation.get(item_id) == generation)
        )

    async def _propose_reason(
        self, item_id: str, vote: Vote, message_id: int, generation: Optional[int] = None,
    ) -> None:
        """Ask the model why the user voted this way and offer ✅ / ✏️ / skip."""
        if not self.reason_callback:
            return
        try:
            proposal = await self.reason_callback(item_id, vote.vote, vote.strength)
        except Exception as e:
            logger.warning(f"Reason proposal failed for {item_id}: {e}")
            return
        if not proposal or not self._vote_is_current(item_id, generation):
            return  # nothing proposed, or the vote changed/was undone meanwhile
        try:
            sent = await self.application.bot.send_message(
                chat_id=self.chat_id,
                text=reason_prompt_text(vote, self._escape_html(proposal)),
                reply_markup=reason_prompt_buttons(item_id),
                parse_mode="HTML",
                reply_to_message_id=message_id,
            )
        except Exception as e:
            logger.warning(f"Could not send reason prompt for {item_id}: {e}")
            return
        if not self._vote_is_current(item_id, generation) or item_id in self._item_prompts:
            # Vote changed while the prompt was in flight, or a newer prompt exists
            try:
                await self.application.bot.delete_message(chat_id=self.chat_id, message_id=sent.message_id)
            except Exception:
                pass
            return
        self._reason_prompts[sent.message_id] = item_id
        self._item_prompts[item_id] = (sent.message_id, proposal)

    async def _confirm_reason(self, item_id: str, reason: str) -> None:
        """Store a confirmed/typed reason on the item's latest vote."""
        reason = reason.strip()
        if item_id in self._pending_feedback:
            # Vote not saved yet (grace period): the save will carry it
            self._pending_reasons[item_id] = reason
        elif self.reason_confirm_callback:
            try:
                await self.reason_confirm_callback(item_id, reason)
            except Exception as e:
                logger.error(f"Error saving reason for {item_id}: {e}")
        prompt = self._item_prompts.pop(item_id, None)
        if prompt:
            self._reason_prompts.pop(prompt[0], None)
            vote = self._item_votes.get(item_id) or Vote("up", 1)
            try:
                await self.application.bot.edit_message_text(
                    chat_id=self.chat_id,
                    message_id=prompt[0],
                    text=reason_done_text(vote, self._escape_html(reason)),
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.debug(f"Reason prompt edit skipped: {e}")

    async def _remove_reason_prompt(self, item_id: str) -> None:
        prompt = self._item_prompts.pop(item_id, None)
        if not prompt:
            return
        self._reason_prompts.pop(prompt[0], None)
        try:
            await self.application.bot.delete_message(
                chat_id=self.chat_id, message_id=prompt[0],
            )
        except Exception as e:
            logger.debug(f"Reason prompt delete skipped: {e}")

    async def _handle_reaction(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Emoji reactions on sent items: 🔥/👍/👎 vote, removal retracts."""
        mr = update.message_reaction
        if mr is None or str(mr.chat.id) != str(self.chat_id):
            return
        item_id = await self._item_for_message(mr.message_id)
        if not item_id:
            return
        vote, retracted = vote_from_reactions(mr.new_reaction, mr.old_reaction)
        # After a restart the in-memory vote is gone; Telegram's old_reaction
        # still tells us what the previous reaction-vote was.
        previous = self._item_votes.get(item_id) or vote_from_reaction_set(mr.old_reaction)

        if retracted or (vote and previous and previous != vote):
            await self._cancel_vote(item_id, restore_keyboard=False)
            if self.retract_vote_callback and (retracted or previous):
                try:
                    await self.retract_vote_callback(item_id)
                except Exception as e:
                    logger.error(f"Error retracting vote for {item_id}: {e}")
            self._item_votes.pop(item_id, None)
            if retracted:
                await self._set_keyboard(
                    mr.message_id,
                    self._make_tweet_buttons(item_id, self._tweet_authors.get(item_id)),
                )
                logger.info(f"Reaction removed → vote retracted: item={item_id}")
                return

        if vote and vote != previous:
            await self._record_vote(
                item_id, vote, mr.message_id, source="reaction", grace=False,
            )

    async def resolve_open(self, item_id: str) -> Optional[str]:
        """Used by the redirect endpoint: record the open, return the real URL."""
        if not self.open_callback:
            return None
        return await self.open_callback(item_id)
