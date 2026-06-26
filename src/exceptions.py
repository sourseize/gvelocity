"""
Custom exceptions for Velocity tracker
"""

class NoDataForDateError(Exception):
    """Raised when iShares has no holdings data for the requested date.

    This happens when the server returns a placeholder CSV with
    `Holdings as of,"-"` and no rows — e.g. weekends, holidays, dates
    outside their retention window, or a date not yet published.
    Callers must NOT fall back to treating this as "today's" data.
    """
    pass