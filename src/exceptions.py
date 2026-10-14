"""
Custom exceptions for Velocity tracker
"""


class VelocityError(Exception):
    """A fatal, *expected* failure with a message meant for the user (bad input, unusable
    database, no price history …).  The entry points catch it, log the message and exit
    non-zero — no traceback."""
    pass


class NoDataForDateError(Exception):
    """Raised when BlackRock has no holdings data for the requested date.

    This happens when the server returns a placeholder CSV with
    `Holdings as of,"-"` and no rows — e.g. weekends, holidays, dates
    outside their retention window, or a date not yet published.
    Callers must NOT fall back to treating this as "today's" data.
    """
    pass
