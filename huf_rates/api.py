import datetime
import logging
from collections.abc import Iterable
from decimal import Decimal
from typing import Final
from zoneinfo import ZoneInfo

import requests

from . import ecb, mnb
from .exceptions import ECBUnavailableError, ParseError, RateMismatchError, RateUnavailableError

logger: Final[logging.Logger] = logging.getLogger(__name__)

DEFAULT_TIMEOUT: Final[float] = 5
# Backtested on USD, EUR and GBP from 2015 to September 2026, MNB's rates were
# at most 2.44% from the ECB's of the same day, and 3.86% from the ECB's of
# the previous day, which is what they're compared with between MNB's fixing
# (around 11:00) and the ECB's (around 16:00).
DEFAULT_ECB_TOLERANCE: Final[Decimal] = Decimal('0.05')

# MNB publishes by the Budapest calendar.
TIMEZONE: Final[ZoneInfo] = ZoneInfo('Europe/Budapest')


def _as_date(value: datetime.date) -> datetime.date:
    """
    Return a date as is, and a datetime as its date in Budapest.

    Args:
        value: A date or datetime. A naive datetime counts as Budapest time.

    Returns:
        The date.
    """
    if not isinstance(value, datetime.datetime):
        return value

    if value.tzinfo is not None:
        value = value.astimezone(TIMEZONE)

    return value.date()


def get_rates(
    currencies: Iterable[str],
    date: datetime.date | None = None,
    ecb_check: bool = False,
    ecb_tolerance: Decimal | float = DEFAULT_ECB_TOLERANCE,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Decimal]:
    """
    Return MNB's HUF exchange rates.

    The rates are the ones of the last day MNB published on or before `date`,
    so weekends and holidays get the previous banking day.

    Each fetch is logged at INFO, with the day MNB published the rates for.

    MNB serves them over http only. With `ecb_check` each rate is compared
    with the ECB's (crossed through EUR, fetched over https) of the same day,
    or of the ECB's last day before it if it hasn't published that day (yet:
    MNB fixes its rates around 11:00, the ECB around 16:00). They typically
    differ by a few tenths of a percent, and by a few percent on market
    shocks, see `DEFAULT_ECB_TOLERANCE`.

    Args:
        currencies: Currency codes, in any case. HUF may be among them.
        date: The day to get the rates of. A datetime counts as its date in
            Budapest. Defaults to today in Budapest.
        ecb_check: Whether to check the rates against the ECB's.
        ecb_tolerance: The largest difference the ECB check allows, as a
            fraction of MNB's rate (0.02 is 2%).
        timeout: Timeout of each request, in seconds.

    Returns:
        HUF per one unit of each currency, keyed by the upper-cased currency
        code. HUF itself is 1.

    Raises:
        RateUnavailableError: MNB didn't publish a rate.
        RateMismatchError: The ECB check failed.
        ECBUnavailableError: The ECB check couldn't run, see
            `ECBUnavailableError`. It carries MNB's unchecked rates.
        ParseError: MNB's response couldn't be parsed or didn't match the
            request.
        requests.RequestException: The request to MNB failed.
    """
    date = datetime.datetime.now(TIMEZONE).date() if date is None else _as_date(date)

    currencies = [c.upper() for c in currencies]
    foreign = [c for c in currencies if c != 'HUF']
    rates: dict[str, Decimal] = {}
    if foreign:
        day, rates = mnb.get_rates(foreign, date, timeout)
        if ecb_check:
            _check_against_ecb_or_raise({day: rates}, ecb_tolerance, timeout)

        logger.info(
            'MNB rates of %s for %s%s: %s',
            day,
            date,
            ", checked against the ECB's" if ecb_check else '',
            ', '.join(f'{c} {rate}' for c, rate in rates.items()),
        )

    if 'HUF' in currencies:
        rates['HUF'] = Decimal(1)

    return rates


def get_rate(
    currency: str,
    date: datetime.date | None = None,
    ecb_check: bool = False,
    ecb_tolerance: Decimal | float = DEFAULT_ECB_TOLERANCE,
    timeout: float = DEFAULT_TIMEOUT,
) -> Decimal:
    """
    Return MNB's HUF exchange rate of one currency.

    Args:
        currency: Currency code, in any case.
        date: See `get_rates`.
        ecb_check: See `get_rates`.
        ecb_tolerance: See `get_rates`.
        timeout: See `get_rates`.

    Returns:
        HUF per one unit of the currency.

    Raises:
        See `get_rates`.
    """
    rates = get_rates(
        [currency],
        date=date,
        ecb_check=ecb_check,
        ecb_tolerance=ecb_tolerance,
        timeout=timeout,
    )
    return rates[currency.upper()]


def get_rates_between(
    currencies: Iterable[str],
    start: datetime.date,
    end: datetime.date,
    ecb_check: bool = False,
    ecb_tolerance: Decimal | float = DEFAULT_ECB_TOLERANCE,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[datetime.date, dict[str, Decimal]]:
    """
    Return MNB's HUF exchange rates of every day it published in a range.

    Unlike `get_rates`, a day MNB didn't publish (a weekend or holiday) is
    left out rather than filled with the previous banking day's rates.

    Args:
        currencies: Currency codes, in any case. HUF may be among them.
        start: First day of the range. A datetime counts as its date in
            Budapest.
        end: Last day of the range. A datetime counts as its date in
            Budapest.
        ecb_check: Whether to check each day's rates against the ECB's, see
            `get_rates`.
        ecb_tolerance: See `get_rates`.
        timeout: See `get_rates`.

    Returns:
        HUF per one unit of each currency, keyed by the upper-cased currency
        code, by day in date order. HUF itself is 1. A day may lack a
        currency MNB didn't publish that day. Empty if MNB published nothing
        in the range, or if HUF is the only currency.

    Raises:
        ValueError: `start` is after `end`.
        RateMismatchError: The ECB check failed.
        ECBUnavailableError: The ECB check couldn't run, see `get_rates`.
        ParseError: MNB's response couldn't be parsed or didn't match the
            request.
        requests.RequestException: The request to MNB failed.
    """
    start, end = _as_date(start), _as_date(end)
    if start > end:
        raise ValueError(f'The range starts ({start}) after it ends ({end})')

    currencies = [c.upper() for c in currencies]
    foreign = [c for c in currencies if c != 'HUF']
    if not foreign:
        return {}

    days = mnb.get_rates_between(foreign, start, end, timeout)
    if ecb_check and days:
        _check_against_ecb_or_raise(days, ecb_tolerance, timeout)

    if 'HUF' in currencies:
        for rates in days.values():
            rates['HUF'] = Decimal(1)

    return days


def _check_against_ecb_or_raise(
    days: dict[datetime.date, dict[str, Decimal]],
    tolerance: Decimal | float,
    timeout: float,
) -> None:
    """
    Run `check_against_ecb`, raising `ECBUnavailableError` if it can't run.

    Args:
        days: See `check_against_ecb`.
        tolerance: See `check_against_ecb`.
        timeout: See `check_against_ecb`.

    Raises:
        RateMismatchError: A rate differs by more than `tolerance`.
        ECBUnavailableError: The ECB couldn't be reached, its response
            couldn't be parsed, or it published no rates to compare with.
    """
    try:
        check_against_ecb(days, tolerance, timeout)
    except (requests.RequestException, ParseError, RateUnavailableError) as e:
        raise ECBUnavailableError(f"The ECB check couldn't run: {e}", days) from e


def check_against_ecb(
    days: dict[datetime.date, dict[str, Decimal]],
    tolerance: Decimal | float,
    timeout: float = DEFAULT_TIMEOUT,
) -> None:
    """
    Check MNB's rates against the ECB's.

    Each day is compared with the ECB's rates of the last day on or before it
    (within `LOOKBACK_DAYS`) that the ECB published all of that day's
    currencies for. The ECB's rates are fetched in one request.

    Args:
        days: MNB's rates of foreign currencies by the day MNB published them
            for, as `get_rates_between` returns them. Nothing is checked if
            it's empty.
        tolerance: The largest difference allowed, as a fraction of MNB's
            rate (0.02 is 2%).
        timeout: Timeout of the request to the ECB, in seconds.

    Raises:
        RateMismatchError: A rate differs by more than `tolerance`.
        RateUnavailableError: The ECB published no rates of all of a day's
            currencies in the `LOOKBACK_DAYS` before it.
        ParseError: The ECB's response couldn't be parsed.
        requests.RequestException: The request failed.
    """
    if not days:
        return

    tolerance = Decimal(str(tolerance))
    lookback = datetime.timedelta(days=ecb.LOOKBACK_DAYS)
    currencies = sorted({c for rates in days.values() for c in rates})
    ecb_days = ecb.get_rates_between(currencies, min(days) - lookback, max(days), timeout)
    for day, rates in days.items():
        published = [
            d
            for d in ecb_days
            if day - lookback <= d <= day and all(c in ecb_days[d] for c in rates)
        ]
        if not published:
            raise RateUnavailableError(
                f'The ECB published no rates for {sorted(rates)} in the '
                f'{ecb.LOOKBACK_DAYS} days before {day}'
            )

        ecb_day = max(published)
        for currency, rate in rates.items():
            ecb_rate = ecb_days[ecb_day][currency]
            deviation = abs(ecb_rate / rate - 1)
            if deviation > tolerance:
                raise RateMismatchError(
                    f"{currency}: MNB's rate of {day} is {rate} HUF, the ECB's of "
                    f'{ecb_day} is {ecb_rate:.4f} HUF ({deviation:.2%} '
                    f'apart, tolerance {tolerance:.2%})'
                )
