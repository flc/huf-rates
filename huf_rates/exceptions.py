import datetime
from decimal import Decimal


class HufRatesError(Exception):
    """Base class of the errors raised by this package."""


class ParseError(HufRatesError):
    """
    A rate source answered with something that couldn't be parsed or didn't
    match the request.
    """


class ResponseMismatchError(ParseError):
    """
    A rate source answered with data that doesn't fit the request, such as
    days outside the requested range. More likely an altered response than
    an outage.
    """


class RateUnavailableError(HufRatesError):
    """No rate was published for a requested currency."""


class RateMismatchError(HufRatesError):
    """MNB's rate is further from the ECB's than the tolerance allows."""


class ECBUnavailableError(HufRatesError):
    """
    The ECB check couldn't run: the ECB couldn't be reached, answered with
    something that couldn't be parsed, or published no rates to compare with.

    Attributes:
        days: MNB's rates that couldn't be checked, by the day MNB published
            them for, as `get_rates_between` returns them (without HUF).
    """

    def __init__(self, message: str, days: dict[datetime.date, dict[str, Decimal]]) -> None:
        super().__init__(message)
        self.days = days
