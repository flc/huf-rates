import math
from typing import Any, Final

from django.conf import settings
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

# Far beyond any real amount, and small enough that converting it at any
# exchange rate stays finite (1e308 EUR, say, overflows to infinity in HUF,
# which the JSON response can't hold).
MAX_AMOUNT: Final[float] = 1e15


class CurrencyConverterSerializer(serializers.Serializer[Any]):
    """Query parameters of `CurrencyConverterView`."""

    from_amount = serializers.FloatField(
        label=_('from amount'),
        min_value=0,
        max_value=MAX_AMOUNT,
    )
    from_currency = serializers.ChoiceField(
        label=_('from currency'),
        choices=settings.CURRENCY_CHOICES,
        required=True,
    )
    to_currency = serializers.ChoiceField(
        label=_('to currency'),
        choices=settings.CURRENCY_CHOICES,
        required=False,
        allow_blank=True,
    )

    def validate_from_amount(self, value: float) -> float:
        """
        Reject NaN and infinity, which the response can't be rendered with.

        `FloatField` accepts them ("nan", "inf", or a number too large for a
        float). Infinity already fails `max_value`, but NaN compares false to
        any bound, so it passes both.

        Args:
            value: The parsed amount.

        Returns:
            The amount.

        Raises:
            ValidationError: The amount isn't finite.
        """
        if not math.isfinite(value):
            raise serializers.ValidationError(_('A finite number is required.'))

        return value
