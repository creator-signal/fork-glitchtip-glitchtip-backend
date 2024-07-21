from django.conf import settings
from django.db.models import Prefetch
from djstripe.models import Price, Product
from rest_framework import viewsets

from .serializers import (
    ProductSerializer,
)


class ProductViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Stripe Product + Prices

    unit_amount is price in cents
    """

    queryset = (
        Product.objects.filter(
            active=True,
            livemode=settings.STRIPE_LIVE_MODE,
            prices__active=True,
            metadata__events__isnull=False,
            metadata__is_public="true",
        )
        .prefetch_related(
            Prefetch("prices", queryset=Price.objects.filter(active=True))
        )
        .distinct()
    )
    serializer_class = ProductSerializer
