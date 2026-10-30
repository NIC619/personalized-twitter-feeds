"""Tests for the offline eval metrics and report."""

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "eval_prompt", Path(__file__).resolve().parent.parent / "scripts" / "eval_prompt.py"
)
eval_prompt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eval_prompt)

auc = eval_prompt.auc
metrics_at = eval_prompt.metrics_at
summarize = eval_prompt.summarize
format_report = eval_prompt.format_report
stored_run = eval_prompt.stored_run


class TestAuc:
    def test_perfect_separation(self):
        assert auc([90, 80], [10, 20]) == 1.0

    def test_random(self):
        assert auc([50, 50], [50, 50]) == 0.5

    def test_inverted(self):
        assert auc([10], [90]) == 0.0

    def test_needs_both_classes(self):
        assert auc([1, 2], []) is None
        assert auc([], [1]) is None


class TestMetricsAt:
    ROWS = [
        {"score": 90, "vote": "up"},
        {"score": 60, "vote": "up"},
        {"score": 75, "vote": "down"},
        {"score": 20, "vote": "down"},
    ]

    def test_values(self):
        m = metrics_at(self.ROWS, 70)
        assert m["recall"] == 0.5          # 1 of 2 ups sent
        assert m["precision"] == 0.5       # 1 up of 2 sent
        assert m["down_blocked"] == 0.5    # 1 of 2 downs blocked
        assert m["send_rate"] == 0.5

    def test_empty(self):
        m = metrics_at([], 70)
        assert m["recall"] is None and m["send_rate"] is None


class TestSummarize:
    def test_bundle(self):
        rows = [
            {"score": 95, "vote": "up", "reason": "Tech content"},
            {"score": 65, "vote": "up", "reason": "Tech content"},
            {"score": 55, "vote": "up", "reason": "Life wisdom", "exploration": True},
            {"score": 30, "vote": "down", "reason": "Event/promo"},
            {"score": None, "vote": "up"},  # unscored rows are dropped
        ]
        s = summarize(rows, 70)
        assert s["n"] == 4 and s["n_up"] == 3 and s["n_down"] == 1
        assert s["auc"] == 1.0
        assert s["at_threshold"]["recall"] == 1 / 3
        assert s["by_reason"]["Tech content"] == {"n": 2, "sent": 1, "recall": 0.5}
        assert s["exploration"] == {"n": 1, "n_up": 1}
        band_50 = next(b for b in s["bands"] if b["band"] == "50-59")
        assert band_50 == {"band": "50-59", "n": 1, "up_share": 1.0}


class TestReport:
    def test_format_report_runs(self):
        runs = [
            {"label": "a", "config": {"model": "m1", "rag": True},
             "rows": [{"score": 80, "vote": "up"}, {"score": 20, "vote": "down"}]},
            {"label": "b", "config": {"model": "m2", "rag": False},
             "rows": [{"score": 40, "vote": "up"}, {"score": 60, "vote": "down"}]},
        ]
        text = format_report(runs, 70)
        assert "AUC" in text
        assert "1.000" in text and "0.000" in text
        assert "Caveat" in text

    def test_stored_run_uses_stored_scores(self):
        runs = [{"label": "a", "rows": [
            {"score": 80, "stored_score": 55, "vote": "up"},
            {"score": 20, "stored_score": None, "vote": "down"},
        ]}]
        stored = stored_run(runs)
        assert stored["label"] == "stored@send"
        assert [r["score"] for r in stored["rows"]] == [55]

    def test_stored_run_none_without_scores(self):
        assert stored_run([{"label": "a", "rows": [{"score": 1, "vote": "up"}]}]) is None
