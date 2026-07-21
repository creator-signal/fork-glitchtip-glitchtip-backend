from django.test import TestCase, override_settings

from apps.users.models import User
from creativesignal.auth import first_user_bootstrap_allowed


class SsoOnlyAuthenticationTestCase(TestCase):
    @override_settings(CREATOR_SIGNAL_SSO_ONLY=True)
    def test_first_user_password_signup_is_denied(self):
        self.assertFalse(first_user_bootstrap_allowed())
        response = self.client.post(
            "/_allauth/browser/v1/auth/signup",
            {"email": "first@example.com", "password": "LongPassword1!"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(User.objects.count(), 0)

    @override_settings(CREATOR_SIGNAL_SSO_ONLY=True)
    def test_password_flows_are_denied_but_admin_break_glass_remains(self):
        for path in (
            "/_allauth/browser/v1/account/password/change",
            "/_allauth/browser/v1/auth/login",
            "/_allauth/browser/v1/auth/password/request",
            "/_allauth/browser/v1/auth/password/reset",
            "/_allauth/browser/v1/auth/reauthenticate",
        ):
            with self.subTest(path=path):
                response = self.client.post(path, {}, content_type="application/json")
                self.assertEqual(response.status_code, 403)
                self.assertEqual(
                    response.json()["errors"][0]["code"],
                    "creator_signal_sso_only",
                )

        self.assertNotEqual(self.client.get("/admin/login/").status_code, 403)

    @override_settings(CREATOR_SIGNAL_SSO_ONLY=False)
    def test_upstream_first_user_bootstrap_is_unchanged_when_disabled(self):
        self.assertTrue(first_user_bootstrap_allowed())
        response = self.client.post(
            "/_allauth/browser/v1/auth/signup",
            {"email": "first@example.com", "password": "LongPassword1!"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
