import datetime
from decimal import Decimal

import pytest
from requests_mock import Mocker

from huf_rates import (
    DEFAULT_ECB_TOLERANCE,
    ParseError,
    RateMismatchError,
    RateUnavailableError,
    ResponseMismatchError,
    check_against_ecb,
    ecb,
    get_rates,
    get_rates_between,
    mnb,
)

from .conftest import ecb_response, mnb_response

MNB_DAYS: dict[str, list[tuple[str, int, str]]] = {
    '2026-09-18': [('EUR', 1, '363,47000'), ('JPY', 100, '213,50000')],
    '2026-09-21': [('EUR', 1, '362,60000'), ('JPY', 100, '214,00000')],
}


def test_uses_last_published_day_with_units_and_decimal_comma(
    requests_mock: Mocker,
) -> None:
    """
    Rates come from MNB's last day on or before the date, per one unit.

    Args:
        requests_mock: Mocks MNB. The ECB isn't mocked, which also pins the
            ECB check being off by default.
    """
    requests_mock.post(mnb.URL, text=mnb_response(MNB_DAYS))

    # A Tuesday before MNB published that day's rates: Monday's apply.
    rates = get_rates(['eur', 'JPY', 'HUF'], date=datetime.date(2026, 9, 22))

    assert rates == {
        'EUR': Decimal('362.6'),
        'JPY': Decimal('2.14'),
        'HUF': Decimal(1),
    }


@pytest.mark.parametrize(
    'days',
    [
        {},
        {'2026-09-21': [('EUR', 1, '362,60000')]},
    ],
    ids=['no-days', 'currency-missing'],
)
def test_unpublished_rate_is_unavailable(
    requests_mock: Mocker, days: dict[str, list[tuple[str, int, str]]]
) -> None:
    """
    No rates in the window, or none for a currency on the latest day, raise.

    Args:
        requests_mock: Mocks MNB answering with `days`.
        days: MNB's days, without a USD rate.
    """
    requests_mock.post(mnb.URL, text=mnb_response(days))

    with pytest.raises(RateUnavailableError):
        get_rates(['EUR', 'USD'], date=datetime.date(2026, 9, 21))


def test_rejects_days_outside_the_requested_range(requests_mock: Mocker) -> None:
    """
    An altered response can't pass off old rates as the requested day's.

    Args:
        requests_mock: Mocks MNB, answering a 2026 request with a 2001 day.
    """
    requests_mock.post(mnb.URL, text=mnb_response({'2001-01-02': [('EUR', 1, '262,00000')]}))

    with pytest.raises(ResponseMismatchError):
        get_rates(['EUR'], date=datetime.date(2026, 9, 22))


def test_rejects_rates_that_are_not_positive(requests_mock: Mocker) -> None:
    """
    A zero rate is a bad response, not a rate to divide by.

    Args:
        requests_mock: Mocks MNB, answering with a zero EUR rate.
    """
    requests_mock.post(mnb.URL, text=mnb_response({'2026-09-21': [('EUR', 1, '0,00000')]}))

    with pytest.raises(ParseError):
        get_rates(['EUR'], date=datetime.date(2026, 9, 21))


def test_ecb_check_uses_tolerance(requests_mock: Mocker) -> None:
    """
    The ECB check fails beyond the default 5% and passes a wider tolerance.

    Args:
        requests_mock: Mocks MNB and the ECB.
    """
    requests_mock.post(mnb.URL, text=mnb_response(MNB_DAYS))
    # 385 HUF per EUR is 6.18% above MNB's 362.60.
    requests_mock.get(
        ecb.URL.format(currencies='HUF'),
        text=ecb_response({'2026-09-21': {'HUF': '385.00'}}),
    )
    date = datetime.date(2026, 9, 21)

    with pytest.raises(RateMismatchError):
        get_rates(['EUR'], date=date, ecb_check=True)
    assert get_rates(['EUR'], date=date, ecb_check=True, ecb_tolerance=0.07)


def test_rates_between_has_each_published_day(requests_mock: Mocker) -> None:
    """
    Every day MNB published is returned, with HUF, and empty days are skipped.

    Args:
        requests_mock: Mocks MNB. The ECB isn't mocked, which also pins the
            ECB check being off by default.
    """
    requests_mock.post(mnb.URL, text=mnb_response({**MNB_DAYS, '2026-09-19': []}))

    days = get_rates_between(
        ['EUR', 'jpy', 'HUF'], datetime.date(2026, 9, 18), datetime.date(2026, 9, 21)
    )

    assert days == {
        datetime.date(2026, 9, 18): {
            'EUR': Decimal('363.47'),
            'JPY': Decimal('2.135'),
            'HUF': Decimal(1),
        },
        datetime.date(2026, 9, 21): {
            'EUR': Decimal('362.6'),
            'JPY': Decimal('2.14'),
            'HUF': Decimal(1),
        },
    }


def test_rates_between_checks_each_day_against_last_ecb_day(requests_mock: Mocker) -> None:
    """
    A day the ECB didn't publish is checked against the ECB's previous day.

    Args:
        requests_mock: Mocks MNB with two days and the ECB with the first.
    """
    requests_mock.post(
        mnb.URL,
        text=mnb_response(
            {
                '2026-09-21': [('EUR', 1, '362,60000')],
                '2026-09-22': [('EUR', 1, '361,73000')],
            }
        ),
    )
    # 369 HUF per EUR is 1.77% above the 21st's 362.60, 2.01% above the 22nd's 361.73.
    requests_mock.get(
        ecb.URL.format(currencies='HUF'),
        text=ecb_response({'2026-09-21': {'HUF': '369.00'}}),
    )

    with pytest.raises(RateMismatchError, match='2026-09-22'):
        get_rates_between(
            ['EUR'],
            datetime.date(2026, 9, 21),
            datetime.date(2026, 9, 22),
            ecb_check=True,
            ecb_tolerance=0.02,
        )


def test_rates_between_checks_each_day_by_its_own_currencies(requests_mock: Mocker) -> None:
    """
    A day is checked if the ECB has its currencies, even when it lacks others.

    Args:
        requests_mock: Mocks MNB with USD on the second day only, and the ECB
            without USD on the first.
    """
    requests_mock.post(
        mnb.URL,
        text=mnb_response(
            {
                '2026-09-21': [('EUR', 1, '362,60000')],
                '2026-09-22': [('EUR', 1, '361,73000'), ('USD', 1, '315,70000')],
            }
        ),
    )
    requests_mock.get(
        ecb.URL.format(currencies='HUF+USD'),
        text=ecb_response(
            {
                '2026-09-21': {'HUF': '362.40'},
                '2026-09-22': {'HUF': '361.58', 'USD': '1.1463'},
            }
        ),
    )

    days = get_rates_between(
        ['EUR', 'USD'], datetime.date(2026, 9, 21), datetime.date(2026, 9, 22), ecb_check=True
    )

    assert list(days) == [datetime.date(2026, 9, 21), datetime.date(2026, 9, 22)]


def test_ecb_check_skips_blank_ecb_values(requests_mock: Mocker) -> None:
    """
    A blank ECB value is skipped, and the check uses the ECB's previous day.

    Args:
        requests_mock: Mocks MNB, and the ECB with a blank USD value on the
            same day.
    """
    requests_mock.post(
        mnb.URL,
        text=mnb_response({'2026-09-21': [('EUR', 1, '362,60000'), ('USD', 1, '315,85000')]}),
    )
    requests_mock.get(
        ecb.URL.format(currencies='HUF+USD'),
        text=ecb_response(
            {
                '2026-09-18': {'HUF': '364.28', 'USD': '1.1460'},
                '2026-09-21': {'HUF': '362.40', 'USD': ''},
            }
        ),
    )

    assert get_rates(['EUR', 'USD'], date=datetime.date(2026, 9, 21), ecb_check=True)


def test_reaches_back_over_the_longest_holiday_gap(requests_mock: Mocker) -> None:
    """
    The 7 days from 23 to 30 December 2024 are bridged, 10 days at most.

    Args:
        requests_mock: Mocks MNB, answering with 23 December only.
    """
    requests_mock.post(mnb.URL, text=mnb_response({'2024-12-23': [('EUR', 1, '411,05000')]}))

    assert get_rates(['EUR'], date=datetime.date(2024, 12, 30)) == {'EUR': Decimal('411.05')}
    assert '<startDate>2024-12-20</startDate>' in requests_mock.request_history[-1].text


def test_aware_datetime_counts_as_its_budapest_date(requests_mock: Mocker) -> None:
    """
    22:30 UTC on the 21st is already the 22nd in Budapest.

    Args:
        requests_mock: Mocks MNB.
    """
    requests_mock.post(mnb.URL, text=mnb_response(MNB_DAYS))

    get_rates(['EUR'], date=datetime.datetime(2026, 9, 21, 22, 30, tzinfo=datetime.UTC))

    assert '<endDate>2026-09-22</endDate>' in requests_mock.request_history[-1].text


def test_check_against_ecb_without_days_checks_nothing(requests_mock: Mocker) -> None:
    """
    Nothing to check makes no request and doesn't fail.

    Args:
        requests_mock: Mocks nothing, so any request would fail.
    """
    check_against_ecb({}, DEFAULT_ECB_TOLERANCE)

    assert requests_mock.call_count == 0
