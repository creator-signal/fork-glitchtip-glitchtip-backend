from allauth.socialaccount.adapter import DefaultSocialAccountAdapter

from apps.users.utils import is_user_registration_open


class CustomSocialAccountAdapter(DefaultSocialAccountAdapter):
    def is_open_for_signup(self, request, sociallogin):
        return is_user_registration_open()
