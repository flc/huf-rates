"""Client for the euro reference rates of the ECB (European Central Bank)."""

import csv
import datetime
import io
from collections.abc import Iterable
from decimal import Decimal
from typing import Final

import requests

from .exceptions import ParseError

URL: Final[str] = 'https://data-api.ecb.europa.eu/service/data/EXR/D.{currencies}.EUR.SP00.A'

# Reaches back over the longest run of TARGET closing days.
LOOKBACK_DAYS: Final[int] = 14


def get_rates_between(
    currencies: Iterable[str],
    start: datetime.date,
    end: datetime.date,
    timeout: float,
) -> dict[datetime.date, dict[str, Decimal]]:
    """
    Return HUF rates crossed from the ECB's euro reference rates of a range.

    The ECB quotes every currency against EUR, so the HUF rate of a currency
    is EUR/HUF divided by EUR/currency.

    Args:
        currencies: Foreign currency codes, in any case.
        start: First day of the range.
        end: Last day of the range.
        timeout: Timeout of the request, in seconds.

    Returns:
        HUF per one unit of each currency, keyed by the upper-cased currency
        code, by day in date order. Only days the ECB published HUF for are
        included, and a day may lack a currency the ECB didn't publish that
        day.

    Raises:
        ParseError: The response couldn't be parsed.
        requests.RequestException: The request failed.
    """
    currencies = [c.upper() for c in currencies]
    series = sorted({'HUF', *currencies} - {'EUR'})
    response = requests.get(
        URL.format(currencies='+'.join(series)),
        params={
            'startPeriod': start.isoformat(),
            'endPeriod': end.isoformat(),
            'format': 'csvdata',
        },
        timeout=timeout,
    )
    # The ECB answers 404 when none of the series has data in the range (it
    # leaves out just the missing ones otherwise).
    if response.status_code == 404:
        return {}

    response.raise_for_status()

    days: dict[datetime.date, dict[str, Decimal]] = {}
    try:
        for row in csv.DictReader(io.StringIO(response.text)):
            # A blank value leaves that currency out of that day, as if the
            # ECB hadn't published it.
            if not row['OBS_VALUE'].strip():
                continue

            day = datetime.date.fromisoformat(row['TIME_PERIOD'])
            value = Decimal(row['OBS_VALUE'])
            if not (value.is_finite() and value > 0):
                raise ValueError(f'Not a positive rate: {row["OBS_VALUE"]}')

            days.setdefault(day, {})[row['CURRENCY']] = value
    except (KeyError, ValueError, ArithmeticError) as e:
        raise ParseError(f"Couldn't parse the ECB's response: {response.text[:500]!r}") from e

    return {
        day: {
            c: rates['HUF'] if c == 'EUR' else rates['HUF'] / rates[c]
            for c in currencies
            if c == 'EUR' or c in rates
        }
        for day, rates in sorted(days.items())
        if 'HUF' in rates
    }
