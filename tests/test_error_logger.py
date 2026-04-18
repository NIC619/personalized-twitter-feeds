"""Tests for the persistent error-log sink and monthly report."""

import io
import logging
import sys
from contextlib import redirect_stdout
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from scripts.error_report import (
    _daily_counts,
    _resolve_month,
    _sparkline,
    run_error_report,
)
from src.error_logger import DatabaseErrorHandler


# ---------- DatabaseErrorHandler ----------

class TestDatabaseErrorHandler:
    def _make_record(self, level: int, msg: str, name: str = "test.logger", exc_info=None):
        return logging.LogRecord(
            name=name,
            level=level,
            pathname=__file__,
            lineno=1,
            msg=msg,
            args=(),
            exc_info=exc_info,
        )

    def test_emits_warning_to_db(self):
        db = MagicMock()
        handler = DatabaseErrorHandler(db, level=logging.WARNING)
        handler.setFormatter(logging.Formatter("%(message)s"))

        handler.emit(self._make_record(logging.WARNING, "something is off", name="src.scheduler"))

        db.save_error_log.assert_called_once()
        kwargs = db.save_error_log.call_args.kwargs
        assert kwargs["source"] == "src.scheduler"
        assert kwargs["level"] == "WARNING"
        assert kwargs["error_type"] is None
        assert kwargs["message"] == "something is off"

    def test_captures_exception_type(self):
        db = MagicMock()
        handler = DatabaseErrorHandler(db)
        handler.setFormatter(logging.Formatter("%(message)s"))

        try:
            raise ValueError("boom")
        except ValueError:
            exc_info = sys.exc_info()

        handler.emit(self._make_record(logging.ERROR, "failed", exc_info=exc_info))

        kwargs = db.save_error_log.call_args.kwargs
        assert kwargs["error_type"] == "ValueError"
        assert kwargs["level"] == "ERROR"

    def test_message_truncated_at_2000(self):
        db = MagicMock()
        handler = DatabaseErrorHandler(db)
        handler.setFormatter(logging.Formatter("%(message)s"))

        handler.emit(self._make_record(logging.WARNING, "x" * 5000))

        kwargs = db.save_error_log.call_args.kwargs
        assert len(kwargs["message"]) == 2000

    def test_reentry_guard_prevents_recursion(self):
        """If save_error_log logs a warning itself, it must not re-enter emit."""
        db = MagicMock()
        handler = DatabaseErrorHandler(db)
        handler.setFormatter(logging.Formatter("%(message)s"))

        def simulate_nested(**_):
            # Simulate a library inside save_error_log raising a warning record.
            handler.emit(handler._make_nested_record() if False else logging.LogRecord(
                name="nested", level=logging.WARNING, pathname=__file__, lineno=1,
                msg="nested", args=(), exc_info=None,
            ))
        db.save_error_log.side_effect = simulate_nested

        # Should not recurse or raise — handleError on the outer record is tolerated.
        handler.emit(self._make_record(logging.WARNING, "outer"))
        # Only the OUTER call reached save_error_log; the nested one was guarded.
        assert db.save_error_log.call_count == 1

    def test_db_failure_falls_back_to_stderr(self):
        """A DB insert failure must not propagate and crash the caller."""
        db = MagicMock()
        db.save_error_log.side_effect = RuntimeError("supabase 502")
        handler = DatabaseErrorHandler(db)
        handler.setFormatter(logging.Formatter("%(message)s"))

        # logging.raiseExceptions defaults to True in dev, which would re-raise
        # via handleError. Disable for this test to simulate production.
        prior = logging.raiseExceptions
        logging.raiseExceptions = False
        try:
            handler.emit(self._make_record(logging.ERROR, "doomed"))
        finally:
            logging.raiseExceptions = prior

        assert db.save_error_log.call_count == 1

    def test_info_below_threshold_is_skipped(self):
        """Handler level=WARNING must not persist INFO records."""
        db = MagicMock()
        handler = DatabaseErrorHandler(db, level=logging.WARNING)
        logger = logging.getLogger("test.info.skip")
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        try:
            logger.info("chatter")
            logger.warning("real")
        finally:
            logger.removeHandler(handler)

        assert db.save_error_log.call_count == 1
        assert db.save_error_log.call_args.kwargs["level"] == "WARNING"


# ---------- error_report helpers ----------

class TestResolveMonth:
    def test_explicit_month(self):
        start, end, label = _resolve_month("2026-03")
        assert start == datetime(2026, 3, 1, tzinfo=timezone.utc)
        assert end == datetime(2026, 4, 1, tzinfo=timezone.utc)
        assert label == "2026-03"

    def test_december_rolls_year(self):
        start, end, _ = _resolve_month("2026-12")
        assert end == datetime(2027, 1, 1, tzinfo=timezone.utc)

    def test_last_is_previous_month(self):
        start, end, _ = _resolve_month("last")
        now = datetime.now(timezone.utc)
        # End must be the first day of the current month.
        expected_end = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        assert end == expected_end
        # Start must be the first day of the previous month.
        assert start.day == 1
        assert start < end

    def test_invalid_spec_raises(self):
        with pytest.raises(ValueError, match="Invalid month spec"):
            _resolve_month("March 2026")


class TestDailyCounts:
    def test_buckets_by_day(self):
        start = datetime(2026, 3, 1, tzinfo=timezone.utc)
        end = datetime(2026, 3, 4, tzinfo=timezone.utc)
        rows = [
            {"logged_at": "2026-03-01T10:00:00+00:00"},
            {"logged_at": "2026-03-01T22:00:00+00:00"},
            {"logged_at": "2026-03-03T01:00:00+00:00"},
        ]
        assert _daily_counts(rows, start, end) == [2, 0, 1]

    def test_tolerates_z_suffix_and_missing(self):
        start = datetime(2026, 3, 1, tzinfo=timezone.utc)
        end = datetime(2026, 3, 2, tzinfo=timezone.utc)
        rows = [
            {"logged_at": "2026-03-01T10:00:00Z"},
            {"logged_at": None},
            {},
        ]
        assert _daily_counts(rows, start, end) == [1]


class TestSparkline:
    def test_empty(self):
        assert _sparkline([]) == ""

    def test_all_zero(self):
        # All-zero input must be all the lowest char, not blank.
        assert _sparkline([0, 0, 0]) == "▁▁▁"

    def test_max_maps_to_top(self):
        out = _sparkline([0, 1, 2, 4])
        assert out[-1] == "█"
        assert len(out) == 4


# ---------- run_error_report integration ----------

class TestRunErrorReport:
    def test_empty_message(self):
        db = MagicMock()
        db.get_error_logs_in_range.return_value = []
        buf = io.StringIO()
        with redirect_stdout(buf):
            run_error_report(db, "2026-03")
        assert "No error or warning records" in buf.getvalue()

    def test_renders_sections(self):
        db = MagicMock()
        db.get_error_logs_in_range.return_value = [
            {
                "logged_at": "2026-03-01T10:00:00+00:00",
                "source": "src.embeddings",
                "level": "WARNING",
                "error_type": None,
                "message": "find_similar_tweets: supabase 502",
            },
            {
                "logged_at": "2026-03-02T11:00:00+00:00",
                "source": "src.embeddings",
                "level": "ERROR",
                "error_type": "HTTPError",
                "message": "embedding request failed",
            },
        ]
        buf = io.StringIO()
        with redirect_stdout(buf):
            run_error_report(db, "2026-03")
        out = buf.getvalue()
        assert "Total records: 2" in out
        assert "WARNING" in out and "ERROR" in out
        assert "src.embeddings" in out
        assert "HTTPError" in out
        assert "Daily trend:" in out
