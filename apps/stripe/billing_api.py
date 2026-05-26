import logging
import re

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.http import HttpRequest, HttpResponseRedirect, JsonResponse
from ipware import get_client_ip
from ninja import Router, Schema
from ninja.errors import Throttled

from .client import fetch_latest_invoice_for_customer
from .exceptions import StripeResourceNotFound
from .tasks import send_license_key_email_by_lookup

logger = logging.getLogger(__name__)

router = Router()

CUSTOMER_ID_PATTERN = re.compile(r"^cus_[A-Za-z0-9]+$")

LICENSE_INVOICE_LIMIT = 10
LICENSE_INVOICE_WINDOW_SECONDS = 60

LICENSE_EMAIL_IP_LIMIT = 10
LICENSE_EMAIL_PER_EMAIL_LIMIT = 3
LICENSE_EMAIL_WINDOW_SECONDS = 60 * 60  # 1 hour


async def _throttle(cache_key: str, limit: int, window_seconds: int):
    """Per-key fixed-window throttle backed by Django cache.

    Raises ninja.errors.Throttled if the limit has been reached. Atomic
    against concurrent callers: uses `aadd` to claim the first slot (only
    one concurrent caller wins) and `aincr` for subsequent slots (atomic
    increment-and-return).
    """
    if await cache.aadd(cache_key, 1, window_seconds):
        return
    try:
        count = await cache.aincr(cache_key)
    except ValueError:
        # Key expired between aadd-False and aincr — vanishingly rare.
        # Treat as a fresh window so we don't 500 the request.
        await cache.aadd(cache_key, 1, window_seconds)
        return
    if count > limit:
        raise Throttled(limit)


@router.get("license-invoice/", auth=None)
async def license_invoice(request: HttpRequest, customer_id: str | None = None):
    """
    Public endpoint: redirect a self-hosted user to their most recent Stripe-
    hosted invoice page.

    The license key on a self-hosted org is the Stripe customer ID (cus_...).
    We look up that customer's most recent invoice and 302 to its
    hosted_invoice_url — Stripe renders an "Invoice paid" receipt page with
    download buttons for paid invoices. This serves as proof of payment for
    the GlitchTip license without us building any custom invoice UI.

    Falls back to the Stripe email-login portal when:
    - the customer ID is unknown to Stripe
    - the customer has no invoices yet
    - the most recent invoice has no hosted URL (e.g. not finalized)
    """
    client_ip, _ = get_client_ip(request)
    await _throttle(
        f"license_invoice_throttle_{client_ip or 'unknown'}",
        LICENSE_INVOICE_LIMIT,
        LICENSE_INVOICE_WINDOW_SECONDS,
    )

    if not customer_id or not CUSTOMER_ID_PATTERN.match(customer_id):
        return JsonResponse({"detail": "Invalid customer ID format"}, status=400)

    try:
        invoice = await fetch_latest_invoice_for_customer(customer_id)
    except StripeResourceNotFound:
        return HttpResponseRedirect(settings.STRIPE_PORTAL_LOGIN_URL)
    except Exception:
        logger.exception("Stripe invoice lookup failed")
        return JsonResponse({"detail": "Unable to look up invoice"}, status=500)

    if invoice is None or not invoice.hosted_invoice_url:
        return HttpResponseRedirect(settings.STRIPE_PORTAL_LOGIN_URL)

    return HttpResponseRedirect(invoice.hosted_invoice_url)


class CustomerByEmailIn(Schema):
    email: str


@router.post("customer-by-email/", auth=None)
async def customer_by_email(request: HttpRequest, payload: CustomerByEmailIn):
    """
    Public endpoint: looks up a Stripe customer by email and, if found, sends
    them their license key (Stripe customer ID) by email.

    Powers the "Find my license key" form on glitchtip.com/support. Customers
    cannot see their own customer IDs in the Stripe UI, so this is the only
    self-serve recovery path.

    Anti-enumeration: always returns 200 {"ok": true} on a syntactically valid
    submission. The Stripe lookup and email send happen out-of-band in a queued
    task so response timing doesn't leak match-vs-no-match.

    Rate-limited on two axes: IP (10/hr, forgiving for corporate NAT) and
    email (3/hr, caps single-inbox harassment regardless of source IP).
    """
    client_ip, _ = get_client_ip(request)

    await _throttle(
        f"license_email_ip_throttle_{client_ip or 'unknown'}",
        LICENSE_EMAIL_IP_LIMIT,
        LICENSE_EMAIL_WINDOW_SECONDS,
    )

    email = (payload.email or "").strip().lower()
    try:
        validate_email(email)
    except ValidationError:
        return JsonResponse({"detail": "Invalid email"}, status=400)

    await _throttle(
        f"license_email_addr_throttle_{email}",
        LICENSE_EMAIL_PER_EMAIL_LIMIT,
        LICENSE_EMAIL_WINDOW_SECONDS,
    )

    await send_license_key_email_by_lookup.aenqueue(email)

    return JsonResponse({"ok": True})
