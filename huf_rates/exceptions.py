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
