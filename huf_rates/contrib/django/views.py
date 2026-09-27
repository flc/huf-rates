from typing import Any, cast

from django.conf import settings
from django.utils.translation import gettext_lazy as _
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException
from rest_framework.metadata import BaseMetadata
from rest_framework.request import Request
from rest_framework.response import Response

from huf_rates import RateUnavailableError

from .exchange import convert_currency, get_exchange_rate_data
from .serializers import CurrencyConverterSerializer


class RatesUnavailable(APIException):
    """The exchange rates can't be fetched and aren't cached."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = _('The exchange rates are unavailable, try again later.')
    default_code = 'rates_unavailable'


class CurrencyConverterView(viewsets.ViewSet):
    """Converts an amount with the current rates of `settings.CURRENCIES`."""

    def get_serializer(self) -> CurrencyConverterSerializer:
        """
        Return the serializer of the query parameters.

        Returns:
            The unvalidated serializer, bound to `request.query_params`.
        """
        return CurrencyConverterSerializer(data=self.request.query_params)

    def list(self, request: Request, format: str | None = None, **kwargs: Any) -> Response:
        """
        Convert `from_amount` of `from_currency` to `to_currency`.

        Args:
            request: Its query parameters are validated by
                `CurrencyConverterSerializer`. Without `to_currency` the
                amount is converted to every other currency.
            format: The format suffix of the URL, if any.
            **kwargs: Other URL keyword arguments, unused.

        Returns:
            `conversions`: a `from_amount`, `from_currency`, `to_amount`,
            `to_currency` object per target currency, and `exchange_rates`:
            a `from_currency`, `to_currency`, `value` object per currency pair.

        Raises:
            ValidationError: The query parameters are invalid (400).
            RatesUnavailable: The rates can't be fetched and aren't cached
                (503).
        """
        ser = self.get_serializer()
        ser.is_valid(raise_exception=True)
        data = ser.validated_data

        from_amount = data['from_amount']
        from_currency = data['from_currency']
        to_currency = data.get('to_currency', None)

        if to_currency:
            currencies = [to_currency]
        else:
            currencies = [c for c in settings.CURRENCIES if c != from_currency]

        try:
            exchange_rate_data = get_exchange_rate_data()
        except RateUnavailableError as e:
            raise RatesUnavailable() from e

        convs = []
        for to_currency in currencies:
            to_amount = convert_currency(
                value=from_amount,
                from_currency=from_currency,
                to_currency=to_currency,
                exchange_rate_data=exchange_rate_data,
            )
            convs.append(
                {
                    'from_amount': from_amount,
                    'from_currency': from_currency,
                    'to_amount': to_amount,
                    'to_currency': to_currency,
                }
            )

        exchange_rates = [
            {'from_currency': k[0], 'to_currency': k[1], 'value': v}
            for k, v in exchange_rate_data.items()
        ]

        return Response(
            {
                'conversions': convs,
                'exchange_rates': exchange_rates,
            }
        )

    # POST is required to be added to methods, otherwise actions do not appear in response
    @action(methods=['GET', 'POST'], detail=False, url_path='schema')
    def get_schema(self, request: Request, **kwargs: Any) -> Response:
        """
        Describe the view's parameters (DRF's OPTIONS metadata).

        Named `get_schema` because `schema` would collide with DRF's schema
        generation.

        Args:
            request: The request.
            **kwargs: URL keyword arguments, unused.

        Returns:
            The metadata `self.metadata_class` determines for the view.
        """
        # The stubs type `metadata_class` as an instance, it's the class.
        meta = cast(type[BaseMetadata], self.metadata_class)()
        data = meta.determine_metadata(request, self)
        return Response(data)
