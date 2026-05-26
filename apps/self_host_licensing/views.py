"""
HTTP views for the self-host licensing feature. Only mounted on deployments
where SELF_HOST_LICENSING_ENABLED is true (i.e. app.glitchtip.com).
"""

import hmac
import json
import logging
import time

from django.conf import settings
from django.core.cache import cache
from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseForbidden,
    JsonResponse,
)
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .issuer import IssuerNotConfigured, mint_for_subscription_id
from .keys import load_trusted_public_keys
from .signing import InvalidLicense, decode
from .verifier import GRACE_PERIOD_SECONDS

logger = logging.getLogger(__name__)


def _verify_stripe_signature(payload: bytes, sig_header: str) -> bool:
    secret = settings.STRIPE_SELF_HOST_WEBHOOK_SECRET
    if not secret:
        logger.error("STRIPE_SELF_HOST_WEBHOOK_SECRET not configured")
        return False
    try:
        parts = {}
        for part in sig_header.split(","):
            key, value = part.strip().split("=", 1)
            parts[key.strip()] = value.strip()
        timestamp = int(parts["t"])
        signature = parts["v1"]
    except (KeyError, ValueError):
        return False

    if time.time() - timestamp > getattr(settings, "STRIPE_WEBHOOK_TOLERANCE", 300):
        return False

    signed = f"{timestamp}.{payload.decode('utf-8')}"
    expected = hmac.new(
        secret.encode(), signed.encode(), digestmod="sha256"
    ).hexdigest()
    return hmac.compare_digest(signature, expected)


def _extract_subscription_id(event_type: str, obj: dict) -> str | None:
    """Pull a subscription id out of a Stripe event object."""
    if event_type == "checkout.session.completed":
        sub = obj.get("subscription")
        if isinstance(sub, str):
            return sub
        return None
    if event_type.startswith("customer.subscription."):
        sub_id = obj.get("id")
        return sub_id if isinstance(sub_id, str) else None
    return None


@csrf_exempt
@require_POST
async def self_host_stripe_webhook_view(request: HttpRequest) -> HttpResponse:
    sig_header = request.META.get("HTTP_STRIPE_SIGNATURE")
    if not sig_header:
        return HttpResponseForbidden("Missing signature header")
    if not _verify_stripe_signature(request.body, sig_header):
        return HttpResponseForbidden("Invalid signature")

    try:
        event = json.loads(request.body)
    except json.JSONDecodeError:
        return HttpResponse(status=200)

    event_id = event.get("id", "")
    event_type = event.get("type", "")
    obj = event.get("data", {}).get("object", {}) or {}

    if event_id and not await cache.aadd(
        f"self_host_licensing_event:{event_id}", 1, 3600
    ):
        return HttpResponse(status=200)

    subscription_id = _extract_subscription_id(event_type, obj)
    if not subscription_id:
        return HttpResponse(status=200)

    try:
        await mint_for_subscription_id(subscription_id, stripe_event_id=event_id)
    except IssuerNotConfigured:
        logger.exception("self-host licensing webhook called but issuer not configured")
        return HttpResponse(status=500)
    except Exception:
        logger.exception(
            "self-host licensing: failed to mint license for %s", subscription_id
        )
        return HttpResponse(status=500)

    return HttpResponse(status=200)


@csrf_exempt
@require_GET
async def refresh_license_view(request: HttpRequest) -> HttpResponse:
    """
    Opt-in phone-home endpoint. A self-host install can pass its current blob
    here and receive a fresh one if the underlying Stripe subscription is
    still active. Used by the optional weekly check-in task on self-hosts
    that enable it; not required for normal operation.
    """
    blob = request.GET.get("blob", "")
    if not blob:
        return JsonResponse({"error": "missing blob"}, status=400)

    try:
        verified = decode(blob, load_trusted_public_keys())
    except InvalidLicense:
        return JsonResponse({"error": "invalid license"}, status=400)

    subscription_id = verified.claims.get("sub")
    if not isinstance(subscription_id, str):
        return JsonResponse({"error": "invalid license"}, status=400)

    rate_key = f"self_host_licensing_refresh:{subscription_id}"
    if not await cache.aadd(rate_key, 1, 60):
        return JsonResponse({"error": "rate limited"}, status=429)

    try:
        license_obj = await mint_for_subscription_id(subscription_id)
    except IssuerNotConfigured:
        return JsonResponse({"error": "not configured"}, status=503)
    except Exception:
        logger.exception("self-host licensing: refresh failed for %s", subscription_id)
        return JsonResponse({"error": "server error"}, status=500)

    if license_obj is None:
        return JsonResponse({"error": "subscription not active"}, status=404)

    # The refresh endpoint signals "we re-issued and emailed you"; the email
    # delivery path is the same one the webhook uses, so the self-host admin
    # still needs to manually paste the new blob. A future iteration could
    # return the blob inline for installs that explicitly opt in, but that
    # requires trusting the TLS termination of whoever hosts the self-host.
    expires_at = int(license_obj.current_period_end.timestamp()) + GRACE_PERIOD_SECONDS
    return JsonResponse(
        {
            "status": license_obj.status,
            "expires_at": expires_at,
            "emailed": True,
        }
    )
