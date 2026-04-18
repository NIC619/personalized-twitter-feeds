"""Persistent error logging: a logging.Handler that writes WARNING+ records to Supabase.

Attached to the root logger alongside the existing console/file handlers, so
normal log output is unaffected. The DB handler is a best-effort sink: if the
insert fails, it falls back to stderr via `handleError` rather than cascading
the failure into the pipeline. A re-entry guard prevents recursion when DB
code itself logs warnings.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.database import DatabaseClient


MAX_MESSAGE_LEN = 2000


class DatabaseErrorHandler(logging.Handler):
    """Logging handler that persists WARNING+ records to the `error_log` table."""

    def __init__(self, db: "DatabaseClient", level: int = logging.WARNING):
        super().__init__(level=level)
        self.db = db
        self._in_write = False

    def emit(self, record: logging.LogRecord) -> None:
        # Skip records emitted while we're already writing to avoid recursion
        # (e.g. if save_error_log or the Supabase client logs a warning).
        if self._in_write:
            return

        try:
            self._in_write = True
            error_type = None
            if record.exc_info and record.exc_info[0] is not None:
                error_type = record.exc_info[0].__name__

            message = self.format(record)
            if len(message) > MAX_MESSAGE_LEN:
                message = message[:MAX_MESSAGE_LEN]

            self.db.save_error_log(
                source=record.name,
                level=record.levelname,
                error_type=error_type,
                message=message,
            )
        except Exception:
            # Never let logging failures break the pipeline. handleError
            # routes to stderr (respecting logging.raiseExceptions).
            self.handleError(record)
        finally:
            self._in_write = False


def attach_db_error_handler(
    db: "DatabaseClient", level: int = logging.WARNING
) -> DatabaseErrorHandler:
    """Attach a DatabaseErrorHandler to the root logger and return it."""
    handler = DatabaseErrorHandler(db, level=level)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger().addHandler(handler)
    return handler
