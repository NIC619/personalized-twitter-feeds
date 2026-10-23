"""Tests for the A/B test report."""

from scripts.ab_test_report import _send_rate, build_ab_report


class FakeDB:
    def __init__(self, rows):
        self.rows = rows

    def get_ab_test_results(self, experiment_id):
        return self.rows


def _row(control, challenger, vote=None):
    return {
        "control_score": control,
        "challenger_score": challenger,
        "control_prompt": "V4",
        "challenger_prompt": "V5",
        "user_vote": vote,
    }


class TestSendRate:
    def test_counts_scores_at_or_above_threshold(self):
        rows = [_row(50, 0), _row(49, 0), _row(80, 0), _row(10, 0)]
        assert _send_rate(rows, "control_score", 50) == 0.5

    def test_empty_results(self):
        assert _send_rate([], "control_score", 50) == 0.0


class TestNoDownvotes:
    def test_withholds_verdict_and_shows_send_rates(self):
        # Challenger has higher recall only because it scores everything higher
        rows = [_row(40, 90, "up") for _ in range(12)] + [_row(10, 90) for _ in range(12)]
        report = build_ab_report(FakeDB(rows), "exp_x", threshold=50)

        assert "No downvotes" in report
        assert "Can't pick a winner without downvotes" in report
        assert "Send rate: control=0.00%, challenger=100.00%" in report
        assert "Consider promoting" not in report

    def test_with_downvotes_uses_f1_verdict(self):
        rows = [_row(40, 90, "up") for _ in range(12)] + [_row(40, 10, "down") for _ in range(3)]
        report = build_ab_report(FakeDB(rows), "exp_x", threshold=50)

        assert "No downvotes" not in report
        assert "Consider promoting the challenger" in report
