import datetime
import logging
from decimal import Decimal, InvalidOperation

import pytest
import requests
from django.core.cache import cache
from django.test import override_settings
from requests_mock import Mocker
from rest_framework.test import APIRequestFactory
from time_machine import TimeMachineFixture

from huf_rates import ECBUnavailableError, api, ecb, mnb
from huf_rates.contrib.django.exchange import (
    DEFAULT_CACHE_MAX_AGE,
    RETRY_DELAY,
    convert_currency,
    get_exchange_rate_data,
    get_mnb_safe,
    get_rates_safe,
    refresh_exchange_rates,
)
from huf_rates.contrib.django.serializers import CurrencyConverterSerializer
from huf_rates.contrib.django.views import CurrencyConverterView

from .conftest import ecb_response, mnb_response


@pytest.fixture(autouse=True)
def today(time_machine: TimeMachineFixture) -> None:
    """
    Make today the day the mocked responses are for.

    The helpers here ask for today's rates, and a response with a day outside
    the lookback is rejected, so without this the tests fail once the mocked
    day is further back than that.

    Args:
        time_machine: Moves the clock.
    """
    time_machine.move_to(datetime.datetime(2026, 9, 21, 12, tzinfo=api.TIMEZONE), tick=False)


def test_falls_back_to_cached_rates_when_mnb_fails(
    requests_mock: Mocker, caplog: pytest.LogCaptureFixture
) -> None:
    """
    Fresh rates fetched earlier are served when MNB fails, logged as an error.

    Args:
        requests_mock: Mocks MNB, answering first with rates, then with 500.
        caplog: Captures the log records.
    """
    requests_mock.post(
        mnb.URL,
        text=mnb_response(
            {
                '2026-09-21': [('USD', 1, '315,85000'), ('EUR', 1, '362,60000')],
            }
        ),
    )
    fresh = get_exchange_rate_data(cache_first=False)

    requests_mock.post(mnb.URL, status_code=500)

    assert get_exchange_rate_data(cache_first=False) == fresh
    assert fresh[('EUR', 'HUF')] == 362.6
    assert [r.levelno for r in caplog.records] == [logging.ERROR]


def test_falling_back_to_stale_rates_is_logged_as_critical(
    requests_mock: Mocker, caplog: pytest.LogCaptureFixture, time_machine: TimeMachineFixture
) -> None:
    """
    Serving cached rates older than the max age is logged as critical.

    Args:
        requests_mock: Mocks MNB, answering first with rates, then with 500.
        caplog: Captures the log records.
        time_machine: Moves the clock past the max age.
    """
    requests_mock.post(
        mnb.URL,
        text=mnb_response({'2026-09-21': [('USD', 1, '315,85000'), ('EUR', 1, '362,60000')]}),
    )
    cached = get_exchange_rate_data(cache_first=False)

    requests_mock.post(mnb.URL, status_code=500)
    time_machine.shift(DEFAULT_CACHE_MAX_AGE + 1)

    assert get_exchange_rate_data() == cached
    assert [r.levelno for r in caplog.records] == [logging.CRITICAL]


def test_waits_after_a_failed_fetch_before_fetching_again(
    requests_mock: Mocker, caplog: pytest.LogCaptureFixture, time_machine: TimeMachineFixture
) -> None:
    """
    After a failed fetch, calls serve stale rates without fetching for a while.

    Args:
        requests_mock: Mocks MNB, answering first with rates, then with 500.
        caplog: Captures the log records.
        time_machine: Moves the clock past the max age, then the retry delay.
    """
    requests_mock.post(
        mnb.URL,
        text=mnb_response({'2026-09-21': [('USD', 1, '315,85000'), ('EUR', 1, '362,60000')]}),
    )
    cached = get_exchange_rate_data(cache_first=False)

    requests_mock.post(mnb.URL, status_code=500)
    time_machine.shift(DEFAULT_CACHE_MAX_AGE + 1)
    requests_before = requests_mock.call_count

    for _ in range(3):
        assert get_exchange_rate_data() == cached
    assert get_exchange_rate_data(cache_first=False) == cached
    assert requests_mock.call_count == requests_before + 1
    assert [r.levelno for r in caplog.records] == [logging.CRITICAL]

    time_machine.shift(RETRY_DELAY + 1)
    get_exchange_rate_data()

    assert requests_mock.call_count == requests_before + 2


def test_failed_fetch_waits_only_for_the_same_currencies(requests_mock: Mocker) -> None:
    """
    A failure only one currency has doesn't hold up fetching others.

    Args:
        requests_mock: Mocks MNB, answering with a USD rate only.
    """
    requests_mock.post(mnb.URL, text=mnb_response({'2026-09-21': [('USD', 1, '315,85000')]}))

    assert get_mnb_safe('UAH') is None
    assert get_rates_safe(['USD']) == {'USD': 315.85}
    assert requests_mock.call_count == 2


def test_cache_first_serves_cached_rates_without_fetching(requests_mock: Mocker) -> None:
    """
    With every rate cached, the default `cache_first` makes no request.

    Args:
        requests_mock: Mocks MNB for the fetch that fills the cache.
    """
    requests_mock.post(
        mnb.URL,
        text=mnb_response({'2026-09-21': [('USD', 1, '315,85000'), ('EUR', 1, '362,60000')]}),
    )
    fresh = get_exchange_rate_data(cache_first=False)
    requests_before = requests_mock.call_count

    assert get_exchange_rate_data() == fresh
    assert requests_mock.call_count == requests_before


def test_cache_first_refetches_stale_rates(
    requests_mock: Mocker, time_machine: TimeMachineFixture
) -> None:
    """
    Cached rates older than the max age are fetched again, not served.

    Args:
        requests_mock: Mocks MNB.
        time_machine: Moves the clock past the max age.
    """
    requests_mock.post(
        mnb.URL,
        text=mnb_response({'2026-09-21': [('USD', 1, '315,85000'), ('EUR', 1, '362,60000')]}),
    )
    get_exchange_rate_data(cache_first=False)
    requests_before = requests_mock.call_count

    time_machine.shift(DEFAULT_CACHE_MAX_AGE + 1)
    get_exchange_rate_data()

    assert requests_mock.call_count == requests_before + 1


def test_cache_first_refetches_rates_cached_without_a_fetch_time(requests_mock: Mocker) -> None:
    """
    Rates cached without a fetch time (by an older version) aren't trusted.

    Args:
        requests_mock: Mocks MNB.
    """
    cache.set_many({'currency:mnb:usd': 300.0, 'currency:mnb:eur': 350.0, 'currency:mnb:huf': 1.0})
    requests_mock.post(
        mnb.URL,
        text=mnb_response({'2026-09-21': [('USD', 1, '315,85000'), ('EUR', 1, '362,60000')]}),
    )

    assert get_exchange_rate_data()[('EUR', 'HUF')] == 362.6


def test_refresh_raises_when_mnb_fails_even_with_cached_rates(requests_mock: Mocker) -> None:
    """
    A refresh that fails raises, so the periodic task running it fails.

    Args:
        requests_mock: Mocks MNB, answering first with rates, then with 500.
    """
    requests_mock.post(
        mnb.URL,
        text=mnb_response({'2026-09-21': [('USD', 1, '315,85000'), ('EUR', 1, '362,60000')]}),
    )
    refresh_exchange_rates()

    requests_mock.post(mnb.URL, status_code=500)

    with pytest.raises(requests.HTTPError):
        refresh_exchange_rates()


def test_get_mnb_safe_returns_default_without_fresh_or_cached_rate(
    requests_mock: Mocker,
) -> None:
    """
    With MNB failing and nothing cached, the caller's default is returned.

    Args:
        requests_mock: Mocks MNB answering 500.
    """
    requests_mock.post(mnb.URL, status_code=500)

    assert get_mnb_safe('EUR', default=400.0) == 400.0


def test_convert_currency_keeps_decimal_amounts_decimal() -> None:
    """A Decimal amount is converted to a Decimal, not a float."""
    converted = convert_currency(Decimal('10'), 'USD', 'EUR', {('USD', 'EUR'): 0.5})

    assert isinstance(converted, Decimal)
    assert converted == Decimal('5')


def test_failed_ecb_check_is_logged_as_critical(
    requests_mock: Mocker, caplog: pytest.LogCaptureFixture
) -> None:
    """
    A failed ECB check falls back to the cache and is logged apart, as critical.

    Args:
        requests_mock: Mocks MNB, and the ECB with rates 7.6% off MNB's.
        caplog: Captures the log records.
    """
    requests_mock.post(
        mnb.URL,
        text=mnb_response({'2026-09-21': [('USD', 1, '315,85000'), ('EUR', 1, '362,60000')]}),
    )
    requests_mock.get(
        ecb.URL.format(currencies='HUF+USD'),
        text=ecb_response({'2026-09-21': {'HUF': '390.00', 'USD': '1.1480'}}),
    )
    cached = get_exchange_rate_data(cache_first=False)

    with override_settings(HUF_RATES_ECB_CHECK=True):
        assert get_exchange_rate_data(cache_first=False) == cached

    assert [r.levelno for r in caplog.records] == [logging.CRITICAL]


def _mock_checked_then_ecb_down(requests_mock: Mocker, usd_after: str) -> None:
    """
    Refresh once with the ECB check passing, then take the ECB down.

    Args:
        requests_mock: Mocks MNB and the ECB.
        usd_after: MNB's USD rate (decimal comma) once the ECB is down.
    """
    requests_mock.post(
        mnb.URL,
        text=mnb_response({'2026-09-21': [('USD', 1, '315,85000'), ('EUR', 1, '362,60000')]}),
    )
    requests_mock.get(
        ecb.URL.format(currencies='HUF+USD'),
        text=ecb_response({'2026-09-21': {'HUF': '362.60', 'USD': '1.1480'}}),
    )
    refresh_exchange_rates()

    requests_mock.post(
        mnb.URL,
        text=mnb_response({'2026-09-21': [('USD', 1, usd_after), ('EUR', 1, '362,60000')]}),
    )
    requests_mock.get(ecb.URL.format(currencies='HUF+USD'), exc=requests.ConnectTimeout)


@override_settings(HUF_RATES_ECB_CHECK=True)
def test_ecb_outage_checks_against_the_last_checked_rates(
    requests_mock: Mocker, caplog: pytest.LogCaptureFixture
) -> None:
    """
    While the ECB is down, MNB's rates close to the last checked ones are used.

    Args:
        requests_mock: Mocks MNB, and the ECB passing the check, then down.
        caplog: Captures the log records.
    """
    _mock_checked_then_ecb_down(requests_mock, '318,00000')

    assert refresh_exchange_rates()[('USD', 'HUF')] == 318.0
    assert [r.levelno for r in caplog.records if r.name.startswith('huf_rates.contrib')] == [
        logging.WARNING
    ]


@override_settings(HUF_RATES_ECB_CHECK=True)
def test_ecb_outage_refuses_rates_far_from_the_last_checked_ones(
    requests_mock: Mocker, caplog: pytest.LogCaptureFixture
) -> None:
    """
    While the ECB is down, rates beyond the tolerance are refused as suspicious.

    Args:
        requests_mock: Mocks MNB, and the ECB passing the check, then down.
        caplog: Captures the log records.
    """
    _mock_checked_then_ecb_down(requests_mock, '340,00000')

    assert get_exchange_rate_data(cache_first=False)[('USD', 'HUF')] == 315.85
    assert [r.levelno for r in caplog.records if r.name.startswith('huf_rates.contrib')] == [
        logging.CRITICAL
    ]


@override_settings(HUF_RATES_ECB_CHECK=True)
def test_ecb_outage_without_checked_rates_fails(requests_mock: Mocker) -> None:
    """
    While the ECB is down, rates never checked can't stand in as the reference.

    Args:
        requests_mock: Mocks MNB, and the ECB down.
    """
    requests_mock.post(
        mnb.URL,
        text=mnb_response({'2026-09-21': [('USD', 1, '315,85000'), ('EUR', 1, '362,60000')]}),
    )
    requests_mock.get(ecb.URL.format(currencies='HUF+USD'), exc=requests.ConnectTimeout)
    with override_settings(HUF_RATES_ECB_CHECK=False):
        refresh_exchange_rates()

    with pytest.raises(ECBUnavailableError):
        refresh_exchange_rates()


def test_response_with_days_outside_the_range_is_logged_as_critical(
    requests_mock: Mocker, caplog: pytest.LogCaptureFixture
) -> None:
    """
    MNB answering with days outside the range is logged like a failed check.

    Args:
        requests_mock: Mocks MNB, answering first with the requested day,
            then with a 2001 day.
        caplog: Captures the log records.
    """
    requests_mock.post(
        mnb.URL,
        text=mnb_response({'2026-09-21': [('USD', 1, '315,85000'), ('EUR', 1, '362,60000')]}),
    )
    cached = get_exchange_rate_data(cache_first=False)

    requests_mock.post(
        mnb.URL,
        text=mnb_response({'2001-01-02': [('USD', 1, '285,00000'), ('EUR', 1, '262,00000')]}),
    )

    assert get_exchange_rate_data(cache_first=False) == cached
    assert [r.levelno for r in caplog.records] == [logging.CRITICAL]


def test_bad_setting_is_not_hidden_by_cached_rates(requests_mock: Mocker) -> None:
    """
    Only failures of the sources fall back to the cache, not a bad setting.

    Args:
        requests_mock: Mocks MNB, so the first fetch fills the cache.
    """
    requests_mock.post(
        mnb.URL,
        text=mnb_response({'2026-09-21': [('USD', 1, '315,85000'), ('EUR', 1, '362,60000')]}),
    )
    get_exchange_rate_data(cache_first=False)

    with override_settings(HUF_RATES_ECB_CHECK=True, HUF_RATES_ECB_TOLERANCE='two percent'):
        with pytest.raises(InvalidOperation):
            get_exchange_rate_data(cache_first=False)


@pytest.mark.parametrize('amount', ['nan', 'inf', '1e309', '1e308'])
def test_converter_rejects_amounts_the_response_cant_hold(amount: str) -> None:
    """
    Amounts that are, or convert to, NaN or infinity fail validation.

    Args:
        amount: A query parameter value `FloatField` parses to NaN or
            infinity, or a finite one that overflows when converted.
    """
    serializer = CurrencyConverterSerializer(data={'from_amount': amount, 'from_currency': 'USD'})

    assert not serializer.is_valid()
    assert 'from_amount' in serializer.errors


def test_converter_converts_to_every_other_currency(requests_mock: Mocker) -> None:
    """
    Without `to_currency`, the amount is converted to each other currency.

    Args:
        requests_mock: Mocks MNB.
    """
    requests_mock.post(
        mnb.URL,
        text=mnb_response({'2026-09-21': [('USD', 1, '315,85000'), ('EUR', 1, '362,60000')]}),
    )
    view = CurrencyConverterView.as_view({'get': 'list'})

    response = view(APIRequestFactory().get('/', {'from_amount': '10', 'from_currency': 'USD'}))

    assert response.status_code == 200
    assert [(c['to_currency'], round(c['to_amount'], 2)) for c in response.data['conversions']] == [
        ('EUR', 8.71),
        ('HUF', 3158.5),
    ]


def test_converter_answers_503_without_fresh_or_cached_rates(requests_mock: Mocker) -> None:
    """
    Rates that can't be had are a temporary outage, not a server error.

    Args:
        requests_mock: Mocks MNB answering 500.
    """
    requests_mock.post(mnb.URL, status_code=500)
    view = CurrencyConverterView.as_view({'get': 'list'})

    response = view(APIRequestFactory().get('/', {'from_amount': '10', 'from_currency': 'USD'}))

    assert response.status_code == 503
