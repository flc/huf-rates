# huf-rates

HUF exchange rates from the Hungarian National Bank (MNB), with an optional
cross-check against the European Central Bank (ECB), plus Django/DRF helpers.

## Usage

```python
import datetime
import huf_rates

huf_rates.get_rates(['USD', 'EUR', 'HUF'])
# {'USD': Decimal('321.80000'), 'EUR': Decimal('366.24000'), 'HUF': Decimal('1')}

huf_rates.get_rate('EUR', date=datetime.date(2026, 9, 20))
# Saturday: Friday's rate
```

Rates are HUF per one unit of the currency. For a weekend or holiday you get
the rate of the last banking day before it, up to 10 days back: a longer gap
raises `RateUnavailableError`.

For a history, `get_rates_between` returns every day MNB published in a range
(weekends and holidays are left out rather than filled in):

```python
huf_rates.get_rates_between(['USD', 'EUR'], datetime.date(2026, 9, 1), datetime.date(2026, 9, 30))
# {datetime.date(2026, 9, 1): {'USD': Decimal(...), 'EUR': Decimal(...)}, ...}
```

## Transport security and the ECB check

MNB's web service works over **http only**: its https address answers 404.
Someone on the network path could therefore alter a rate. To catch that,
turn on the ECB check, which fetches the ECB's euro reference rates over
https, crosses them through EUR and compares:

```python
huf_rates.get_rates(['USD', 'EUR'], ecb_check=True, ecb_tolerance=0.05)
```

`get_rates_between` takes the same two arguments and checks every day, each
against the ECB's rates of that day (or its last day before, if the ECB didn't
publish it), with a single request to the ECB.

`RateMismatchError` is raised if any rate differs by more than the tolerance
(a fraction: `0.05` is 5%, the default). The ECB's rates are not MNB's: MNB
fixes its rates around 11:00 and the ECB publishes around 16:00, so in between
today's MNB rates are compared with the ECB's of the previous day. Backtested
on USD, EUR and GBP from 2015 to September 2026, the two typically differ by
0.15 to 0.3%, but on market shocks by much more: up to 2.44% on the same day
and 3.86% against the ECB's previous day (USD, 10 March 2026). A 2% tolerance
would have failed on about 4 days a year, so don't set it much below 5%. The
check is off by default. If the ECB can't be reached while it is on, the call
fails. So does the check for a currency the ECB doesn't publish (MNB publishes
more, e.g. UAH): it raises `RateUnavailableError`, so leave the check off for
those.

## Django

Install with the `django` extra. The helpers in
`huf_rates.contrib.django.exchange` work with `settings.CURRENCIES`:
`get_exchange_rate_data`, `get_exchange_rate`, `convert_currency`,
`get_rates_safe`, `get_mnb_safe`, `refresh_exchange_rates`,
`get_exchange_rate_data_for_date`.
Fetched rates are cached without expiry (as `currency:mnb:<code>`, with the
fetch time as `currency:mnb-fetched:<code>`) and used as the fallback when MNB
can't be reached or the ECB check fails.

Cached rates count as fresh for `HUF_RATES_CACHE_MAX_AGE`. Reading the cache
first serves only fresh rates and fetches otherwise, so rates don't go stale
if the periodic refresh stops running. Falling back to rates that aren't fresh
is logged as critical, as are a failed ECB check and a response that doesn't
fit the request; other fallbacks are logged as errors. Alert on the critical
ones. For a minute after a fetch of the same currencies failed, the cached
rates are served without fetching, so an outage doesn't hold up every call for
the timeout.
`refresh_exchange_rates`, meant for a periodic task, always fetches and raises
when it can't, instead of falling back, so the task fails.

Optional settings:

| setting | default | |
| --- | --- | --- |
| `HUF_RATES_ECB_CHECK` | `False` | run the ECB check |
| `HUF_RATES_ECB_TOLERANCE` | `0.05` | allowed difference, as a fraction |
| `HUF_RATES_TIMEOUT` | `5` | request timeout in seconds |
| `HUF_RATES_CACHE_MAX_AGE` | `86400` | seconds cached rates count as fresh for |

`huf_rates.contrib.django.urls` serves the DRF currency converter view.

## Development

```sh
uv run pytest
uv run mypy
uv run ruff check
uv run ruff format
```
