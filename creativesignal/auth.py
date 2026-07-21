from django.conf import settings


def sso_only_enabled() -> bool:
    """Return whether Creator Signal's fail-closed SSO mode is active."""
    return bool(getattr(settings, "CREATOR_SIGNAL_SSO_ONLY", False))


def first_user_bootstrap_allowed() -> bool:
    """Keep upstream first-user bootstrap available outside our SSO image."""
    return not sso_only_enabled()
