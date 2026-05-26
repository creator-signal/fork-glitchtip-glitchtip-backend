"""
License blob verification for self-host installs.

This module runs on every self-host deployment. It reads GLITCHTIP_LICENSE_KEY
from Django settings (which in turn reads the env var), verifies the signature
against the embedded public keys, checks expiration with a grace period, and
returns a small status dict suitable for exposing on the settings API.
"""

import logging
import time
from dataclasses import dataclass

from django.conf import settings

from .keys import load_trusted_public_keys
from .signing import InvalidLicense, decode

logger = logging.getLogger(__name__)

# Continue treating a license as active for this many seconds past its `exp`
# claim, so a late renewal email or short payment-retry window doesn't instantly
# re-enable the support banner. Stripe dunning runs ~3 weeks, so 14 days is a
# safe middle ground.
GRACE_PERIOD_SECONDS = 14 * 24 * 60 * 60


@dataclass
class LicenseStatus:
    active: bool
    plan: str | None = None
    expires_at: int | None = None

    def as_dict(self) -> dict:
        return {
            "active": self.active,
            "plan": self.plan,
            "expires_at": self.expires_at,
        }


_INACTIVE = LicenseStatus(active=False)


def get_license_status() -> LicenseStatus:
    blob = getattr(settings, "GLITCHTIP_LICENSE_KEY", None)
    if not blob:
        return _INACTIVE

    try:
        verified = decode(blob, load_trusted_public_keys())
    except InvalidLicense as e:
        logger.warning("self-host license failed verification: %s", e)
        return _INACTIVE

    claims = verified.claims
    exp = claims.get("exp")
    if not isinstance(exp, int):
        return _INACTIVE
    if exp + GRACE_PERIOD_SECONDS < int(time.time()):
        return _INACTIVE

    plan = claims.get("pln") if isinstance(claims.get("pln"), str) else None
    return LicenseStatus(active=True, plan=plan, expires_at=exp)
