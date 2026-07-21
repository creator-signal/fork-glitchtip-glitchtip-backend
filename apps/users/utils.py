from django.conf import settings

from apps.users.models import User
from creativesignal.auth import first_user_bootstrap_allowed


def is_user_registration_open() -> bool:
    return settings.ENABLE_USER_REGISTRATION or (
        first_user_bootstrap_allowed() and not User.objects.exists()
    )


async def ais_user_registration_open() -> bool:
    return settings.ENABLE_USER_REGISTRATION or (
        first_user_bootstrap_allowed() and not await User.objects.aexists()
    )


def is_social_apps_user_registration_open() -> bool:
    return settings.ENABLE_SOCIAL_APPS_USER_REGISTRATION or (
        first_user_bootstrap_allowed() and not User.objects.exists()
    )


async def ais_social_apps_user_registration_open() -> bool:
    return (
        settings.ENABLE_SOCIAL_APPS_USER_REGISTRATION
        or (first_user_bootstrap_allowed() and not await User.objects.aexists())
    )
