from django.http import JsonResponse
from django.utils.deprecation import MiddlewareMixin

from creativesignal.auth import sso_only_enabled

PASSWORD_AUTH_PATHS = frozenset(
    {
        "/_allauth/browser/v1/account/password/change",
        "/_allauth/browser/v1/auth/login",
        "/_allauth/browser/v1/auth/password/request",
        "/_allauth/browser/v1/auth/password/reset",
        "/_allauth/browser/v1/auth/reauthenticate",
        "/_allauth/browser/v1/auth/signup",
    }
)


class SsoOnlyMiddleware(MiddlewareMixin):
    """Deny password account flows while leaving OIDC and admin break-glass intact."""

    def process_request(self, request):
        if not sso_only_enabled() or request.path not in PASSWORD_AUTH_PATHS:
            return None
        return JsonResponse(
            {
                "status": 403,
                "errors": [
                    {
                        "code": "creator_signal_sso_only",
                        "message": "Password authentication is disabled; use ZITADEL.",
                    }
                ],
            },
            status=403,
        )
