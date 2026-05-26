from django.db import models

from glitchtip.base_models import CreatedModel


class IssuedLicense(CreatedModel):
    """
    Record of a license blob issued for a Stripe subscription.

    Stripe is the source of truth; this table exists only for support-lookup
    convenience ("what did we last send to this customer?") and for webhook
    idempotency via `last_stripe_event_id`.
    """

    stripe_subscription_id = models.CharField(max_length=255, unique=True)
    stripe_customer_id = models.CharField(max_length=255, db_index=True)
    email = models.EmailField()
    plan = models.CharField(max_length=255)
    status = models.CharField(max_length=32)
    current_period_end = models.DateTimeField()
    last_issued_at = models.DateTimeField(auto_now=True)
    last_stripe_event_id = models.CharField(max_length=255, blank=True, default="")

    def __str__(self) -> str:
        return f"{self.stripe_subscription_id} ({self.email})"
