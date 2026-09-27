"""Client for the exchange rate web service of MNB (Magyar Nemzeti Bank)."""

import datetime
from collections.abc import Iterable
from decimal import Decimal
from typing import Final
from xml.etree import ElementTree
from xml.sax.saxutils import escape

import requests

from .exceptions import ParseError, RateUnavailableError, ResponseMismatchError

# MNB serves the SOAP calls over http only: the same path over https answers
# 404, and the WSDL (which does load over https) names this http address too.
URL: Final[str] = 'http://www.mnb.hu/arfolyamok.asmx'
NAMESPACE: Final[str] = 'http://www.mnb.hu/webservices/'
SOAP_ACTION: Final[str] = f'{NAMESPACE}MNBArfolyamServiceSoap/GetExchangeRates'

# Reaches back over the longest run of Hungarian non-banking days: from 2015
# to September 2026 that was 7 days (23 to 30 December 2019 and 2024). No
# further, as it also caps how old a rate a response with its latest days
# dropped (MNB is served over http) can pass off as current.
LOOKBACK_DAYS: Final[int] = 10

ENVELOPE: Final[str] = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
    '<soap:Body>'
    f'<GetExchangeRates xmlns="{NAMESPACE}">'
    '<startDate>{start}</startDate>'
    '<endDate>{end}</endDate>'
    '<currencyNames>{currencies}</currencyNames>'
    '</GetExchangeRates>'
    '</soap:Body>'
    '</soap:Envelope>'
)


def get_rates_between(
    currencies: Iterable[str],
    start: datetime.date,
    end: datetime.date,
    timeout: float,
) -> dict[datetime.date, dict[str, Decimal]]:
    """
    Return MNB's rates of every day it published in a date range.

    Args:
        currencies: Foreign currency codes, in any case.
        start: First day of the range.
        end: Last day of the range.
        timeout: Timeout of the request, in seconds.

    Returns:
        HUF per one unit of each currency, keyed by the upper-cased currency
        code, by day in date order. Days MNB published no rates for are left
        out, and a day may lack a currency MNB didn't publish that day.

    Raises:
        ResponseMismatchError: The response has a day outside the range.
        ParseError: The response couldn't be parsed.
        requests.RequestException: The request failed.
    """
    currencies = [c.upper() for c in currencies]
    envelope = ENVELOPE.format(
        start=start.isoformat(),
        end=end.isoformat(),
        currencies=escape(','.join(currencies)),
    )
    response = requests.post(
        URL,
        data=envelope.encode(),
        headers={
            'Content-Type': 'text/xml; charset=utf-8',
            'SOAPAction': f'"{SOAP_ACTION}"',
        },
        timeout=timeout,
    )
    response.raise_for_status()

    # The result is an XML document of its own, sent as the text of the
    # GetExchangeRatesResult element.
    try:
        root = ElementTree.fromstring(response.content)
        result = root.find(f'.//{{{NAMESPACE}}}GetExchangeRatesResult')
        if result is None or result.text is None:
            raise ValueError('No GetExchangeRatesResult in the response')

        document = ElementTree.fromstring(result.text)
        days: dict[datetime.date, dict[str, Decimal]] = {}
        for day in document.iter('Day'):
            rates = {}
            for rate in day.iter('Rate'):
                if not rate.text:
                    continue

                # Rates use a decimal comma and are quoted per `unit` pieces
                # of the currency (e.g. 100 JPY).
                value = Decimal(rate.text.replace(',', '.')) / int(rate.get('unit', '1'))
                if not (value.is_finite() and value > 0):
                    raise ValueError(f'Not a positive rate: {rate.text}')

                rates[rate.get('curr', '')] = value

            days[datetime.date.fromisoformat(day.get('date', ''))] = rates
    except (ElementTree.ParseError, ValueError, ArithmeticError) as e:
        raise ParseError(f"Couldn't parse MNB's response: {response.content[:500]!r}") from e

    # A day outside the range means the response isn't an answer to this
    # request (an altered one could pass off old rates as current).
    outside = sorted(day for day in days if not start <= day <= end)
    if outside:
        raise ResponseMismatchError(f'MNB answered with days outside {start} to {end}: {outside}')

    return {day: days[day] for day in sorted(days) if days[day]}


def get_rates(
    currencies: Iterable[str],
    date: datetime.date,
    timeout: float,
) -> tuple[datetime.date, dict[str, Decimal]]:
    """
    Return MNB's rates of the last day it published on or before a date.

    Args:
        currencies: Foreign currency codes, in any case.
        date: The day to get the rates of.
        timeout: Timeout of the request, in seconds.

    Returns:
        The day the rates were published for, and HUF per one unit of each
        currency, keyed by the upper-cased currency code.

    Raises:
        RateUnavailableError: MNB published no rate for a currency on that
            day, or no rates at all in the `LOOKBACK_DAYS` before it.
        ParseError: The response couldn't be parsed.
        requests.RequestException: The request failed.
    """
    currencies = [c.upper() for c in currencies]
    start = date - datetime.timedelta(days=LOOKBACK_DAYS)
    days = get_rates_between(currencies, start, date, timeout)
    if not days:
        raise RateUnavailableError(
            f'MNB published no rates for {currencies} between {start} and {date}'
        )

    day = max(days)
    missing = [c for c in currencies if c not in days[day]]
    if missing:
        raise RateUnavailableError(f'MNB published no rate for {missing} on {day}')

    return day, {c: days[day][c] for c in currencies}
