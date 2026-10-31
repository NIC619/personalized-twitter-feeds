"""Tests for profile compilation helpers."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from src.profile_compiler import (
    ProfileCompiler,
    _core_sections,
    format_record,
    group_signals,
    render_preferences_md,
    scoring_profile,
)


def _row(kind, value=None, domain="ethereum-protocol", text="t", reason=None, strength=1):
    return {
        "kind": kind, "value": value, "strength": strength, "reason": reason,
        "created_at": "2026-10-01T00:00:00Z",
        "items": {"text": text, "title": None, "author_username": "a", "domain": domain},
    }


class TestGroupSignals:
    def test_groups_by_domain_and_kind(self):
        rows = [
            _row("vote", "up", text="liked"),
            _row("vote", "down", text="meh", domain="crypto"),
            _row("note", reason="remember this", text="noted"),
            _row("vote", "up", domain=None, text="untagged"),
        ]
        g = group_signals(rows)
        assert [e["text"] for e in g["ethereum-protocol"]["up"]] == ["liked"]
        assert [e["text"] for e in g["crypto"]["down"]] == ["meh"]
        assert g["ethereum-protocol"]["notes"][0]["reason"] == "remember this"
        assert g["other"]["up"][0]["text"] == "untagged"

    def test_title_and_text_joined(self):
        row = _row("vote", "up")
        row["items"]["title"] = "Title"
        row["items"]["text"] = "Body"
        assert group_signals([row])["ethereum-protocol"]["up"][0]["text"] == "Title\nBody"


class TestFormatRecord:
    def test_marks_and_reasons(self):
        group = {
            "up": [{"text": "x", "reason": "deep", "strength": 2, "author": "a"},
                   {"text": "y", "reason": None, "strength": 1, "author": None}],
            "down": [{"text": "z", "reason": "promo", "strength": 1, "author": "b"}],
            "notes": [{"text": "x", "reason": "revisit", "strength": 1, "author": None}],
        }
        out = format_record(group)
        assert "🔥 (@a): x — reason: \"deep\"" in out
        assert "- 👍: y" in out
        assert "👎 (@b): z — reason: \"promo\"" in out
        assert '"revisit"' in out


class TestScoringProfile:
    PROFILES = {
        "overall": {"content": "## Overall\nYou like depth.", "stats": {}},
        "ethereum-protocol": {"content": "### What you want\n- preconfs\n### What you skip\n- promo\n### Nuances\n- n", "stats": {"share": 0.8}},
        "culture": {"content": "### What you want\n- films\n### What you skip\n- gossip", "stats": {"share": 0.2}},
    }

    def test_fallback_without_profiles(self):
        assert scoring_profile({}, "baseline") == "baseline"

    def test_orders_overall_then_by_share(self):
        text = scoring_profile(self.PROFILES, "baseline")
        assert text.index("Overall") < text.index("## ethereum-protocol") < text.index("## culture")

    def test_budget_trims_to_core_sections(self):
        text = scoring_profile(self.PROFILES, "baseline", budget=100)
        assert "Overall" in text
        assert "Nuances" not in text  # trimmed

    def test_core_sections(self):
        md = "### What you want\n- a\n### Nuances\n- b\n### What you skip\n- c"
        assert _core_sections(md) == "### What you want\n- a\n### What you skip\n- c"


class TestRender:
    def test_render_preferences_md(self):
        md = render_preferences_md({
            "overall": {"content": "## Overall\nx", "version": 2, "stats": {}},
            "ai": {"content": "### What you want\n- agents", "version": 1, "stats": {"up": 3, "down": 1}},
        })
        assert md.startswith("# Preferences")
        assert "## Overall" in md
        assert "## ai _(v1, 3 up / 1 down)_" in md


class TestCompileAll:
    def _compiler(self, rows, previous=None):
        db = MagicMock()
        db.get_signals_for_profile.return_value = rows
        db.get_latest_profiles.return_value = previous or {}
        client = MagicMock()
        block = SimpleNamespace(type="tool_use", name="write_profile", input={"content": "PROFILE"})
        client.messages.create.return_value = SimpleNamespace(content=[block])
        return ProfileCompiler(client, "model-x", db), db, client

    def test_skips_thin_domains_and_writes_overall(self):
        rows = [_row("vote", "up", text=f"t{i}") for i in range(10)] + [_row("vote", "down", domain="crypto")]
        compiler, db, client = self._compiler(rows)

        written = compiler.compile_all(min_votes=8)

        assert set(written) == {"ethereum-protocol", "overall"}
        saved = [c[0][0] for c in db.save_profile.call_args_list]
        assert saved == ["ethereum-protocol", "overall"]
        # domain prompt + overall prompt
        assert client.messages.create.call_count == 2

    def test_previous_version_included_in_prompt(self):
        rows = [_row("vote", "up", text=f"t{i}") for i in range(10)]
        compiler, db, client = self._compiler(
            rows, previous={"ethereum-protocol": {"content": "OLD PROFILE"}}
        )
        compiler.compile_all(min_votes=8)
        prompt = client.messages.create.call_args_list[0][1]["messages"][0]["content"]
        assert "OLD PROFILE" in prompt
