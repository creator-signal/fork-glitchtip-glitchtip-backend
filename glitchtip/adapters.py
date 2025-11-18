from allauth.account.adapter import DefaultAccountAdapter
from allauth.account.internal.flows.login import record_authentication
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.conf import settings

from apps.users.utils import (
    is_social_apps_user_registration_open,
    is_user_registration_open,
)


class CustomSocialAccountAdapter(DefaultSocialAccountAdapter):
    def is_open_for_signup(self, request, sociallogin):
        return is_social_apps_user_registration_open()


class CustomDefaultAccountAdapter(DefaultAccountAdapter):
    def render_mail(self, template_prefix, email, context, headers=None):
        headers = headers or {}
        default_headers = self.get_default_mail_headers()
        for key, value in default_headers.items():
            if key not in headers:
                headers[key] = value
        return super().render_mail(template_prefix, email, context, headers)

    def get_default_mail_headers(self):
        glitchtip_hostname = (
            getattr(getattr(settings, "GLITCHTIP_URL", None), "hostname", None)
            or "glitchtip"
        )
        return {
            "List-Id": f"<system.{glitchtip_hostname}>",
            "X-Mailer": "GlitchTip",
            "Precedence": "bulk",
        }

    def is_open_for_signup(self, request):
        return is_user_registration_open()

    def save_user(self, request, user, form, commit=True):
        # Consider a signup a form of authentication
        user = super().save_user(request, user, form, commit)
        if commit:
            record_authentication(request, user, method="signup")
        return user
