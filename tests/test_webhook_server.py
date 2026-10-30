"""Tests for the webhook + redirect server."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from tornado.httpclient import HTTPClientError
from tornado.testing import AsyncHTTPTestCase

from src.webhook_server import make_app, redirect_url


class TestRedirectUrl:
    def test_none_without_base(self):
        assert redirect_url(None, "42") is None
        assert redirect_url("", "42") is None

    def test_builds_link(self):
        assert redirect_url("https://app.up.railway.app/", "42") == "https://app.up.railway.app/r/42"


class RedirectServerTest(AsyncHTTPTestCase):
    def get_app(self):
        self.application = MagicMock()
        self.application.update_queue = MagicMock()
        self.application.update_queue.put = AsyncMock()
        self.resolve = AsyncMock(side_effect=lambda item_id: {
            "42": "https://x.com/a/status/42",
        }.get(item_id))
        return make_app(self.application, "secret", self.resolve)

    def test_redirects_known_item(self):
        resp = self.fetch("/r/42", follow_redirects=False)
        assert resp.code == 302
        assert resp.headers["Location"] == "https://x.com/a/status/42"
        assert resp.headers["Cache-Control"] == "no-store"
        self.resolve.assert_awaited_once_with("42")

    def test_unknown_item_404(self):
        resp = self.fetch("/r/nope", follow_redirects=False)
        assert resp.code == 404

    def test_resolver_error_404(self):
        self.resolve.side_effect = RuntimeError("db down")
        resp = self.fetch("/r/42", follow_redirects=False)
        assert resp.code == 404

    def test_health(self):
        assert self.fetch("/health").code == 200

    def test_webhook_rejects_bad_json(self):
        resp = self.fetch("/secret", method="POST", body="not json")
        assert resp.code == 400

    def test_webhook_wrong_path_404(self):
        resp = self.fetch("/other", method="POST", body="{}")
        assert resp.code == 404
