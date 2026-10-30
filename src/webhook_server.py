"""HTTP server for production: Telegram webhook + click-tracking redirects.

Replaces python-telegram-bot's built-in webhook server so the same port can
also serve ``/r/<item_id>`` links. Opening such a link records an "open"
signal for the item and redirects to the real URL.
"""

import json
import logging
import re
from typing import Awaitable, Callable, Optional

import tornado.web
from telegram import Update
from telegram.ext import Application

logger = logging.getLogger(__name__)

ResolveFn = Callable[[str], Awaitable[Optional[str]]]
"""async (item_id) -> destination URL or None; also records the open."""


class _UpdateHandler(tornado.web.RequestHandler):
    # Named ptb_app: tornado's RequestHandler already owns ``application``.
    def initialize(self, ptb_app: Application) -> None:
        self.ptb_app = ptb_app

    async def post(self) -> None:
        try:
            data = json.loads(self.request.body)
        except json.JSONDecodeError:
            self.set_status(400)
            return
        update = Update.de_json(data, self.ptb_app.bot)
        if update is None:
            self.set_status(400)
            return
        await self.ptb_app.update_queue.put(update)
        self.set_status(200)


class _RedirectHandler(tornado.web.RequestHandler):
    def initialize(self, resolve: ResolveFn) -> None:
        self.resolve = resolve

    async def get(self, item_id: str) -> None:
        if not item_id or len(item_id) > 128:
            self.set_status(404)
            return
        try:
            url = await self.resolve(item_id)
        except Exception as e:  # never let tracking break the link
            logger.warning(f"Redirect resolve failed for {item_id}: {e}")
            url = None
        if not url:
            self.set_status(404)
            self.write("Unknown item")
            return
        self.set_header("Cache-Control", "no-store")
        self.redirect(url, status=302)


class _HealthHandler(tornado.web.RequestHandler):
    def get(self) -> None:
        self.write("ok")


def make_app(application: Application, url_path: str, resolve: ResolveFn) -> tornado.web.Application:
    """Build the tornado app. ``url_path`` is the secret webhook path."""
    return tornado.web.Application([
        (rf"/{re.escape(url_path)}", _UpdateHandler, {"ptb_app": application}),
        (r"/r/([^/]+)", _RedirectHandler, {"resolve": resolve}),
        (r"/health", _HealthHandler),
    ])


def redirect_url(base_url: Optional[str], item_id: str) -> Optional[str]:
    """Tracking link for an item, or None when tracking is not configured."""
    if not base_url:
        return None
    return f"{base_url.rstrip('/')}/r/{item_id}"
