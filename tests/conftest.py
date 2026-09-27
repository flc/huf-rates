from collections.abc import Iterator
from xml.sax.saxutils import escape

import django
import pytest
from django.conf import settings

settings.configure(
    CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
    CURRENCIES=['USD', 'EUR', 'HUF'],
    CURRENCY_CHOICES=[(c, c) for c in ['USD', 'EUR', 'HUF']],
    # DRF's default anonymous user needs django.contrib.auth installed.
    REST_FRAMEWORK={'UNAUTHENTICATED_USER': None},
)
django.setup()


def mnb_response(days: dict[str, list[tuple[str, int, str]]]) -> str:
    """
    Build a response of MNB's GetExchangeRates operation.

    Args:
        days: `(currency, unit, value)` rates by ISO date, values with a
            decimal comma as MNB sends them.

    Returns:
        The SOAP response body.
    """
    document = '<MNBExchangeRates>{}</MNBExchangeRates>'.format(
        ''.join(
            '<Day date="{}">{}</Day>'.format(
                day,
                ''.join(
                    f'<Rate unit="{unit}" curr="{curr}">{value}</Rate>'
                    for curr, unit, value in rates
                ),
            )
            for day, rates in days.items()
        )
    )
    return (
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
        '<GetExchangeRatesResponse xmlns="http://www.mnb.hu/webservices/">'
        f'<GetExchangeRatesResult>{escape(document)}</GetExchangeRatesResult>'
        '</GetExchangeRatesResponse></s:Body></s:Envelope>'
    )


def ecb_response(days: dict[str, dict[str, str]]) -> str:
    """
    Build a csvdata response of the ECB's API.

    Args:
        days: Units of each currency per EUR, by ISO date.

    Returns:
        The CSV response body.
    """
    rows = ['CURRENCY,TIME_PERIOD,OBS_VALUE']
    rows += [f'{c},{day},{v}' for day, rates in days.items() for c, v in rates.items()]
    return '\n'.join(rows)


@pytest.fixture(autouse=True)
def clear_cache() -> Iterator[None]:
    """
    Clear Django's cache after each test.

    Yields:
        Nothing, runs the test in between.
    """
    from django.core.cache import cache

    yield
    cache.clear()
