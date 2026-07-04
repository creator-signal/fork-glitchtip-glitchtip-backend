import ipaddress
import json
import time
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.cache import cache
from django.core.exceptions import DisallowedRedirect
from django.http import HttpResponseBadRequest, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.views.decorators.http import require_http_methods

from apps.api_tokens.models import generate_token

from .provider import AUTH_CODE_LIFETIME, _grant_cache_key

SIGNED_DATA_MAX_AGE = 600  # 10 minutes


def _is_loopback_host(host: str) -> bool:
    """True for ``localhost`` and IPv4/IPv6 loopback literals (127.0.0.0/8, ::1)."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class MCPOAuthRedirect(HttpResponseRedirect):
    """Redirect to an MCP client's OAuth callback, allowing custom URI schemes.

    MCP clients are typically native apps (RFC 8252) that complete the flow via
    custom-scheme callbacks like ``cursor://…``. Django's HttpResponseRedirect
    only permits http/https/ftp, so widen the allowlist from settings. The
    target redirect_uri is server-signed and was validated against the
    registered client during ``authorize``, so it is not attacker-controlled
    here.

    Plaintext ``http`` is additionally restricted to loopback hosts, matching
    the MCP spec's Communication Security rule that redirect URIs must be either
    ``localhost`` or use HTTPS.
    """

    def __init__(self, redirect_to, *args, **kwargs):
        # Read settings per-instance so @override_settings works in tests.
        self.allowed_schemes = settings.GLITCHTIP_MCP_OAUTH_REDIRECT_SCHEMES
        super().__init__(redirect_to, *args, **kwargs)
        parsed = urlsplit(str(redirect_to))
        if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname or ""):
            raise DisallowedRedirect(
                f"Unsafe non-loopback http redirect to host '{parsed.hostname}'"
            )


@login_required
@require_http_methods(["GET", "POST"])
async def oauth_consent(request):
    try:
        data = signing.loads(request.GET.get("data", ""), max_age=SIGNED_DATA_MAX_AGE)
    except (signing.BadSignature, signing.SignatureExpired):
        return HttpResponseBadRequest("Invalid or expired authorization request.")

    if request.method == "GET":
        return TemplateResponse(
            request,
            "oauth/consent.html",
            {
                "client_id": data.get("client_id", ""),
                "scopes": data.get("scopes") or [],
                "signed_data": request.GET["data"],
            },
        )

    # POST — user approved
    user = await request.auser()
    code = generate_token()
    now = int(time.time())
    grant_data = json.dumps(
        {
            "client_id": data["client_id"],
            "user_id": user.id,
            "scopes": data.get("scopes") or [],
            "expires_at": now + AUTH_CODE_LIFETIME,
            "code_challenge": data["code_challenge"],
            "redirect_uri": data["redirect_uri"],
            "redirect_uri_provided_explicitly": data[
                "redirect_uri_provided_explicitly"
            ],
            "resource": data.get("resource"),
        }
    )
    await cache.aset(_grant_cache_key(code), grant_data, AUTH_CODE_LIFETIME)

    redirect_uri = data["redirect_uri"]
    separator = "&" if "?" in redirect_uri else "?"
    location = f"{redirect_uri}{separator}code={code}"
    if data.get("state"):
        location += f"&state={data['state']}"
    return MCPOAuthRedirect(location)
