"""
Django helpers on top of `huf_rates.get_rates`, working with the currencies
listed in `settings.CURRENCIES`. Rates here are floats.

Settings (all optional):

- `HUF_RATES_ECB_CHECK`: check MNB's rates against the ECB's (default False)
- `HUF_RATES_ECB_TOLERANCE`: allowed difference as a fraction (default 0.05)
- `HUF_RATES_TIMEOUT`: request timeout in seconds (default 5)
- `HUF_RATES_CACHE_MAX_AGE`: seconds cached rates count as fresh for (default
  a day)
"""

import datetime
import itertools
import logging
import time
from collections.abc import Iterable
from decimal import Decimal
from typing import Final

import requests
from django.conf import settings
from django.core.cache import cache

from huf_rates import (
    DEFAULT_ECB_TOLERANCE,
    DEFAULT_TIMEOUT,
    HufRatesError,
    RateMismatchError,
    RateUnavailableError,
    ResponseMismatchError,
    get_rates,
)

logger: Final[logging.Logger] = logging.getLogger(__name__)

# Cache key of a currency's rate, by its lower-cased code. The other keys
# are kept out of this pattern, so no currency code can collide with them.
CACHE_KEY: Final[str] = 'currency:mnb:{}'
# Cache key of when a currency's rate was fetched (a Unix timestamp), by its
# lower-cased code.
FETCHED_CACHE_KEY: Final[str] = 'currency:mnb-fetched:{}'

# Cache key set for `RETRY_DELAY` seconds after a fetch fails, so the calls
# in the meantime use the cached rates rather than each waiting for the
# timeout of a fetch that would most likely fail too. By the sorted,
# lower-cased, comma-joined codes of the fetch's currencies: a failure only
# some currencies have (e.g. one the ECB doesn't publish) mustn't hold up
# fetching others.
FAILED_CACHE_KEY: Final[str] = 'currency:mnb-failed:{}'
RETRY_DELAY: Final[int] = 60

DEFAULT_CACHE_MAX_AGE: Final[float] = 24 * 60 * 60

ExchangeRateData = dict[tuple[str, str], float]


def _fetch_rates(
    currencies: Iterable[str],
    date: datetime.date | None = None,
) -> dict[str, float]:
    """
    Fetch MNB's rates with the options set in the settings.

    Args:
        currencies: Currency codes, in any case. HUF may be among them.
        date: The day to get the rates of. Defaults to today.

    Returns:
        HUF per one unit of each currency, keyed by the upper-cased code.

    Raises:
        See `huf_rates.get_rates`.
    """
    rates = get_rates(
        currencies,
        date=date,
        ecb_check=getattr(settings, 'HUF_RATES_ECB_CHECK', False),
        ecb_tolerance=getattr(settings, 'HUF_RATES_ECB_TOLERANCE', DEFAULT_ECB_TOLERANCE),
        timeout=getattr(settings, 'HUF_RATES_TIMEOUT', DEFAULT_TIMEOUT),
    )
    return {c: float(rate) for c, rate in rates.items()}


def _cache_rates(rates: dict[str, float]) -> None:
    """
    Cache rates without expiry, with the time they were fetched.

    Args:
        rates: HUF per one unit of each currency, keyed by the currency code.
    """
    now = time.time()
    cache.set_many(
        {
            **{CACHE_KEY.format(c.lower()): rate for c, rate in rates.items()},
            **{FETCHED_CACHE_KEY.format(c.lower()): now for c in rates},
        },
        timeout=None,
    )


def _pairs(rates: dict[str, float]) -> ExchangeRateData:
    """
    Turn HUF rates into the rate of every ordered pair of the currencies.

    Args:
        rates: HUF per one unit of each currency.

    Returns:
        How many units of the second currency one unit of the first is worth,
        for every pair of different currencies.
    """
    return {(c1, c2): rates[c1] / rates[c2] for c1, c2 in itertools.permutations(rates, 2)}


def get_rates_safe(
    currencies: Iterable[str],
    cache_first: bool = False,
) -> dict[str, float]:
    """
    Return the current rates, falling back to the cached ones.

    Fetched rates are cached without expiry. If they can't be fetched (a
    request fails, a response is bad, or the ECB check fails), the cached
    rates are returned instead. Other errors propagate. For `RETRY_DELAY`
    seconds after a fetch of the same currencies failed, the cached rates are
    returned without fetching.

    A failed ECB check and a response that doesn't fit the request are logged
    as critical. So is falling back to rates that are not fresh (fetched more
    than `HUF_RATES_CACHE_MAX_AGE` ago, or at an unknown time): they may be
    going onto documents, so a refresh that keeps failing must not go
    unnoticed. Other failures are logged as errors.

    Args:
        currencies: Currency codes, in any case. HUF may be among them.
        cache_first: Return the cached rates without fetching when every
            currency has a fresh one.

    Returns:
        HUF per one unit of each currency, keyed by the upper-cased code.

    Raises:
        RateUnavailableError: The rates couldn't be fetched, or a fetch of the
            same currencies failed less than `RETRY_DELAY` seconds ago, and
            some currency has never been cached.
    """
    keys = {c.upper(): CACHE_KEY.format(c.lower()) for c in currencies}
    fetched_keys = [FETCHED_CACHE_KEY.format(c.lower()) for c in keys]
    failed_key = FAILED_CACHE_KEY.format(','.join(sorted(c.lower() for c in keys)))
    cached_by_key = cache.get_many([*keys.values(), *fetched_keys, failed_key])
    cached = {c: cached_by_key[k] for c, k in keys.items() if k in cached_by_key}
    missing = [c for c in keys if c not in cached]

    # Rates cached without a fetch time (e.g. by an older version) count as
    # not fresh.
    max_age = getattr(settings, 'HUF_RATES_CACHE_MAX_AGE', DEFAULT_CACHE_MAX_AGE)
    fetched: list[float] = [cached_by_key[k] for k in fetched_keys if k in cached_by_key]
    oldest = min(fetched, default=None) if len(fetched) == len(fetched_keys) else None
    fresh = oldest is not None and time.time() - oldest <= max_age

    if cache_first and not missing and fresh:
        return cached

    # The failure that started the wait was logged already.
    if failed_key in cached_by_key:
        if missing:
            raise RateUnavailableError(
                f'No cached rate for {missing}, and fetching failed less than {RETRY_DELAY}s ago'
            )

        return cached

    # Only failures of the sources fall back to the cache: a bug or a bad
    # setting must not hide behind the cached rates.
    try:
        rates = _fetch_rates(keys)
    except (HufRatesError, requests.RequestException) as e:
        cache.set(failed_key, True, timeout=RETRY_DELAY)
        fallback = f'and no cached rate for {missing}' if missing else 'using the cached ones'
        if isinstance(e, (RateMismatchError, ResponseMismatchError)):
            # Signs of an altered response rather than an outage, so they're
            # logged apart from network failures, to alert on.
            logger.critical('Suspicious exchange rates, %s', fallback, exc_info=True)
        elif not missing and not fresh:
            logger.critical(
                "Couldn't get fresh exchange rates, using cached ones that are not fresh "
                '(fetched %s)',
                'at an unknown time' if oldest is None else f'{time.time() - oldest:.0f}s ago',
                exc_info=True,
            )
        else:
            logger.exception("Couldn't get fresh exchange rates, %s", fallback)

        if missing:
            raise RateUnavailableError(f'No cached rate for {missing}') from e

        return cached

    _cache_rates(rates)
    return rates


def get_mnb_safe(
    currency: str,
    cache_first: bool = False,
    default: float | None = None,
) -> float | None:
    """
    Return the current rate of one currency, falling back to the cached one.

    Args:
        currency: Currency code, in any case.
        cache_first: Return the cached rate without fetching if it's fresh.
        default: Returned when the rate can't be fetched and was never cached.

    Returns:
        HUF per one unit of the currency, or `default`.
    """
    try:
        return get_rates_safe([currency], cache_first=cache_first)[currency.upper()]
    except RateUnavailableError:
        return default


def get_exchange_rate_data(cache_first: bool = True) -> ExchangeRateData:
    """
    Return the current rates between the currencies of `settings.CURRENCIES`.

    Args:
        cache_first: See `get_rates_safe`.

    Returns:
        How many units of the second currency one unit of the first is worth,
        for every pair of different currencies.

    Raises:
        RateUnavailableError: See `get_rates_safe`.
    """
    return _pairs(get_rates_safe(settings.CURRENCIES, cache_first=cache_first))


def refresh_exchange_rates() -> ExchangeRateData:
    """
    Fetch and cache the current rates, e.g. from a periodic task.

    Unlike `get_exchange_rate_data`, a failure is raised rather than covered
    by the cached rates, so a periodic task that can't refresh them fails.

    Returns:
        See `get_exchange_rate_data`.

    Raises:
        See `huf_rates.get_rates`.
    """
    rates = _fetch_rates(settings.CURRENCIES)
    _cache_rates(rates)
    return _pairs(rates)


def get_exchange_rate_data_for_date(date: datetime.date) -> ExchangeRateData:
    """
    Return the rates of a day between the currencies of `settings.CURRENCIES`.

    Unlike `get_exchange_rate_data`, this neither reads nor writes the cache.

    Args:
        date: The day to get the rates of. Weekends and holidays get the
            previous banking day.

    Returns:
        See `get_exchange_rate_data`.

    Raises:
        See `huf_rates.get_rates`.
    """
    return _pairs(_fetch_rates(settings.CURRENCIES, date))


def get_exchange_rate(
    from_currency: str,
    to_currency: str,
    exchange_rate_data: ExchangeRateData | None = None,
) -> float:
    """
    Return the rate between two currencies of `settings.CURRENCIES`.

    Args:
        from_currency: Upper-cased code of the currency to convert from.
        to_currency: Upper-cased code of the currency to convert to.
        exchange_rate_data: Rates from `get_exchange_rate_data`. Fetched
            when not given.

    Returns:
        How many units of `to_currency` one unit of `from_currency` is worth.

    Raises:
        AssertionError: A currency isn't in `settings.CURRENCIES`.
        RateUnavailableError: See `get_rates_safe`.
    """
    if from_currency == to_currency:
        return 1.0

    assert from_currency in settings.CURRENCIES, 'from currency is not supported'
    assert to_currency in settings.CURRENCIES, 'to currency is not supported'

    data = exchange_rate_data or get_exchange_rate_data()
    return data[(from_currency, to_currency)]


def convert_currency(
    value: float | Decimal,
    from_currency: str,
    to_currency: str,
    exchange_rate_data: ExchangeRateData | None = None,
) -> float | Decimal:
    """
    Convert an amount between two currencies of `settings.CURRENCIES`.

    Args:
        value: The amount in `from_currency`.
        from_currency: See `get_exchange_rate`.
        to_currency: See `get_exchange_rate`.
        exchange_rate_data: See `get_exchange_rate`.

    Returns:
        The amount in `to_currency`, a Decimal if `value` is one.

    Raises:
        See `get_exchange_rate`.
    """
    rate = get_exchange_rate(from_currency, to_currency, exchange_rate_data)
    if isinstance(value, Decimal):
        return Decimal(rate) * value

    return rate * value
