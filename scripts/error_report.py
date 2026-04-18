#!/usr/bin/env python3
"""Monthly error log report.

Reads WARNING+ records captured by `src.error_logger.DatabaseErrorHandler`
and prints a plain-text summary: totals, daily sparkline, breakdowns by
source / level / error_type, top repeated messages, and the most recent rows.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Iterable

logger = logging.getLogger(__name__)


# Braille-block sparkline — one char per day, height scales to the max.
_SPARK_CHARS = "▁▂▃▄▅▆▇█"


def _resolve_month(spec: str) -> tuple[datetime, datetime, str]:
    """Resolve a `YYYY-MM` or `last` spec into (start, end, label).

    Both bounds are timezone-aware UTC. `end` is exclusive (first instant of
    next month), matching `logged_at < end` semantics.
    """
    now = datetime.now(timezone.utc)
    if spec.lower() == "last":
        first_of_this = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = first_of_this
        # Step back one day, then snap to month start
        prev = first_of_this - timedelta(days=1)
        start = prev.replace(day=1)
    else:
        try:
            start = datetime.strptime(spec, "%Y-%m").replace(tzinfo=timezone.utc)
        except ValueError as e:
            raise ValueError(
                f"Invalid month spec '{spec}'. Use 'YYYY-MM' or 'last'."
            ) from e
        # First of next month
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)
    label = start.strftime("%Y-%m")
    return start, end, label


def _sparkline(counts: list[int]) -> str:
    if not counts:
        return ""
    hi = max(counts)
    if hi == 0:
        return _SPARK_CHARS[0] * len(counts)
    step = hi / (len(_SPARK_CHARS) - 1)
    out = []
    for c in counts:
        idx = 0 if c == 0 else min(len(_SPARK_CHARS) - 1, int(round(c / step)))
        out.append(_SPARK_CHARS[idx])
    return "".join(out)


def _daily_counts(rows: Iterable[dict], start: datetime, end: datetime) -> list[int]:
    """Build per-day counts from `start` (inclusive) to `end` (exclusive)."""
    days = (end - start).days
    counts = [0] * days
    for r in rows:
        ts = r.get("logged_at")
        if not ts:
            continue
        # Supabase returns ISO strings; tolerate both offsets and naive UTC.
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        idx = (dt - start).days
        if 0 <= idx < days:
            counts[idx] += 1
    return counts


def _format_recent(rows: list[dict], limit: int = 10) -> list[str]:
    """Format the most recent rows as compact lines."""
    lines = []
    for r in rows[-limit:][::-1]:
        ts = r.get("logged_at", "")[:19].replace("T", " ")
        level = r.get("level", "?")
        source = r.get("source", "?")
        err = r.get("error_type") or "-"
        msg = (r.get("message") or "").split("\n", 1)[0][:120]
        lines.append(f"  {ts}  {level:<8}  {source:<24}  {err:<20}  {msg}")
    return lines


def run_error_report(db, month_spec: str) -> None:
    """Print a monthly error log report to stdout."""
    start, end, label = _resolve_month(month_spec)

    print(f"\n{'=' * 60}")
    print(f"Error Log Report: {label}  ({start.date()} → {(end - timedelta(days=1)).date()})")
    print(f"{'=' * 60}\n")

    rows = db.get_error_logs_in_range(start, end)
    if not rows:
        print("No error or warning records in this range.\n")
        return

    # --- Totals by level ---
    level_counts = Counter(r.get("level", "?") for r in rows)
    total = sum(level_counts.values())
    print(f"Total records: {total}")
    for lvl in ("WARNING", "ERROR", "CRITICAL"):
        if level_counts.get(lvl):
            print(f"  {lvl:<8}  {level_counts[lvl]}")
    other = total - sum(level_counts.get(l, 0) for l in ("WARNING", "ERROR", "CRITICAL"))
    if other:
        print(f"  OTHER     {other}")
    print()

    # --- Daily trend ---
    counts = _daily_counts(rows, start, end)
    print("Daily trend:")
    print(f"  {_sparkline(counts)}  (max={max(counts)}/day)")
    print()

    # --- Top sources ---
    source_counts = Counter(r.get("source", "?") for r in rows)
    print("Top sources:")
    for source, n in source_counts.most_common(10):
        print(f"  {n:>5}  {source}")
    print()

    # --- Top error types (only rows that have one) ---
    type_counts = Counter(
        r["error_type"] for r in rows if r.get("error_type")
    )
    if type_counts:
        print("Top error types:")
        for et, n in type_counts.most_common(10):
            print(f"  {n:>5}  {et}")
        print()

    # --- Top repeated messages (first line only, truncated) ---
    msg_counts: Counter[str] = Counter()
    for r in rows:
        msg = (r.get("message") or "").split("\n", 1)[0][:100]
        msg_counts[msg] += 1
    print("Top messages:")
    for msg, n in msg_counts.most_common(10):
        print(f"  {n:>5}  {msg}")
    print()

    # --- Recent records ---
    print("Most recent (up to 10):")
    for line in _format_recent(rows, limit=10):
        print(line)
    print()
