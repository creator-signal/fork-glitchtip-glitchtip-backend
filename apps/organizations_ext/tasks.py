from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.db.models import Q
from djstripe.models import Product

from .email import InvitationEmail, MetQuotaEmail
from .models import Organization


def get_free_tier_organizations_with_event_count():
    """
    Free tier means either no plan selected or only inactive plan
    """
    return Organization.objects.with_event_counts().filter(
        Q(djstripe_customers__isnull=True)
        | Q(
            djstripe_customers__subscriptions__plan__amount=0,
            djstripe_customers__subscriptions__status="active",
        )
        | Q(
            djstripe_customers__subscriptions__status="canceled",
        )
        & ~Q(  # Avoid exclude, it doesn't filter relations the same way
            djstripe_customers__subscriptions__plan__amount__gt=0,
            djstripe_customers__subscriptions__status="active",
        )
    )


@shared_task
def check_organization_throttle(organization_id: int):
    # Pretty fast: random.random() < 1/5000
    if not cache.add(f"org-throttle-{organization_id}", True):
        return  # Recent check already performed

    org = Organization.objects.with_event_counts().get(id=organization_id)
    plan_events: int | None = (
        Product.objects.filter(
            plan__subscriptions__customer__subscriber=org,
            plan__subscriptions__status="active",
        )
        .values_list("metadata__events", flat=True)
        .first()
    )
    org_throttle = 0
    if plan_events is None or org.total_event_count > plan_events * 2:
        org_throttle = 100
    elif org.total_event_count > plan_events * 1.5:
        org_throttle = 50
    elif org.total_event_count > plan_events:
        org_throttle = 10

    if org.event_throttle_rate != org_throttle:
        # TODO add email notifications
        org.event_throttle_rate = org_throttle
        org.save(update_fields=["event_throttle_rate"])


@shared_task
def set_organization_throttle():
    """Determine if organization should be throttled"""
    # Currently throttling only happens if billing is enabled and user has free plan.
    if settings.BILLING_ENABLED:
        events_max = settings.BILLING_FREE_TIER_EVENTS
        free_tier_organizations = get_free_tier_organizations_with_event_count()

        # Throttle when over event limit or has no plan/canceled plan
        orgs_over_quota = (
            free_tier_organizations.filter(
                is_accepting_events=True,
            )
            .filter(
                Q(total_event_count__gt=events_max)
                | Q(djstripe_customers__isnull=True)
                | Q(djstripe_customers__subscriptions__status="canceled")
            )
            .select_related("owner__organization_user")
        )
        for org in orgs_over_quota:
            send_email_met_quota.delay(org.pk)
        orgs_over_quota.update(is_accepting_events=False)

        # To unthrottled, must have active subscription and less events than max
        free_tier_organizations.exclude(djstripe_customers__isnull=True).filter(
            djstripe_customers__subscriptions__status="active",
            is_accepting_events=False,
            total_event_count__lte=events_max,
        ).update(is_accepting_events=True)

        # paid accounts should always be active at this time
        Organization.objects.filter(
            is_accepting_events=False,
            djstripe_customers__subscriptions__plan__amount__gt=0,
            djstripe_customers__subscriptions__status="active",
        ).update(is_accepting_events=True)


@shared_task
def send_email_met_quota(organization_id: int):
    MetQuotaEmail(pk=organization_id).send_email()


@shared_task
def send_email_invite(org_user_id: int, token: str):
    InvitationEmail(pk=org_user_id, token=token).send_email()
