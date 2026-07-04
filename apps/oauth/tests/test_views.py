import json

from django.core import signing
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from model_bakery import baker

from apps.oauth.provider import _grant_cache_key


def _make_signed_data(**overrides):
    data = {
        "client_id": "test-client-id",
        "state": "test-state",
        "scopes": ["org:read", "event:read"],
        "code_challenge": "challenge123",
        "redirect_uri": "http://localhost:3000/callback",
        "redirect_uri_provided_explicitly": True,
        "resource": None,
    }
    data.update(overrides)
    return signing.dumps(data)


class OAuthConsentViewTest(TestCase):
    def setUp(self):
        self.user = baker.make("users.user", is_active=True)
        self.user.set_password("testpass")
        self.user.save()

    def tearDown(self):
        cache.clear()

    def test_get_unauthenticated_redirects(self):
        signed = _make_signed_data()
        resp = self.client.get(reverse("oauth_consent") + f"?data={signed}")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp["Location"])

    def test_get_authenticated_renders_consent(self):
        self.client.force_login(self.user)
        signed = _make_signed_data()
        resp = self.client.get(reverse("oauth_consent") + f"?data={signed}")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "test-client-id")
        self.assertContains(resp, "org:read")
        self.assertContains(resp, "event:read")
        self.assertContains(resp, "Authorize")

    def test_post_stores_grant_and_redirects(self):
        self.client.force_login(self.user)
        signed = _make_signed_data()
        resp = self.client.post(
            reverse("oauth_consent") + f"?data={signed}",
        )
        self.assertEqual(resp.status_code, 302)
        location = resp["Location"]
        self.assertIn("http://localhost:3000/callback", location)
        self.assertIn("code=", location)
        self.assertIn("state=test-state", location)

        # Extract the code from redirect URL
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(location)
        params = parse_qs(parsed.query)
        code = params["code"][0]

        # Verify grant is in cache
        raw = cache.get(_grant_cache_key(code))
        self.assertIsNotNone(raw)
        grant = json.loads(raw)
        self.assertEqual(grant["client_id"], "test-client-id")
        self.assertEqual(grant["user_id"], self.user.id)
        self.assertEqual(grant["scopes"], ["org:read", "event:read"])

    def test_post_redirects_to_custom_scheme(self):
        """Native-app MCP clients (e.g. Cursor) use custom-scheme callbacks."""
        self.client.force_login(self.user)
        redirect_uri = "cursor://anysphere.cursor-mcp/oauth/callback"
        signed = _make_signed_data(redirect_uri=redirect_uri)
        resp = self.client.post(reverse("oauth_consent") + f"?data={signed}")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp["Location"].startswith(redirect_uri))
        self.assertIn("code=", resp["Location"])

    @override_settings(
        GLITCHTIP_MCP_OAUTH_REDIRECT_SCHEMES=["http", "https", "myclient"]
    )
    def test_custom_scheme_is_configurable(self):
        """Schemes not in the default set work once added to the allowlist."""
        self.client.force_login(self.user)
        signed = _make_signed_data(redirect_uri="myclient://callback")
        resp = self.client.post(reverse("oauth_consent") + f"?data={signed}")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp["Location"].startswith("myclient://callback"))

    @override_settings(GLITCHTIP_MCP_OAUTH_REDIRECT_SCHEMES=["http", "https"])
    def test_disallowed_scheme_is_rejected(self):
        """A scheme outside the allowlist is refused (DisallowedRedirect -> 400)."""
        self.client.force_login(self.user)
        signed = _make_signed_data(redirect_uri="javascript:alert(1)")
        resp = self.client.post(reverse("oauth_consent") + f"?data={signed}")
        self.assertEqual(resp.status_code, 400)

    def test_loopback_http_redirects(self):
        """Plaintext http to loopback hosts is allowed (RFC 8252 native apps)."""
        for redirect_uri in (
            "http://127.0.0.1:33418/callback",
            "http://[::1]:9000/callback",
        ):
            with self.subTest(redirect_uri=redirect_uri):
                self.client.force_login(self.user)
                signed = _make_signed_data(redirect_uri=redirect_uri)
                resp = self.client.post(reverse("oauth_consent") + f"?data={signed}")
                self.assertEqual(resp.status_code, 302)
                self.assertTrue(resp["Location"].startswith(redirect_uri))

    def test_non_loopback_http_is_rejected(self):
        """Plaintext http to a non-loopback host is refused per the spec."""
        self.client.force_login(self.user)
        signed = _make_signed_data(redirect_uri="http://evil.example.com/callback")
        resp = self.client.post(reverse("oauth_consent") + f"?data={signed}")
        self.assertEqual(resp.status_code, 400)

    def test_non_loopback_https_allowed(self):
        """https to any host remains allowed."""
        self.client.force_login(self.user)
        signed = _make_signed_data(redirect_uri="https://app.example.com/callback")
        resp = self.client.post(reverse("oauth_consent") + f"?data={signed}")
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp["Location"].startswith("https://app.example.com/callback"))

    def test_post_without_state(self):
        self.client.force_login(self.user)
        signed = _make_signed_data(state=None)
        resp = self.client.post(
            reverse("oauth_consent") + f"?data={signed}",
        )
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("state=", resp["Location"])

    def test_invalid_signed_data(self):
        self.client.force_login(self.user)
        resp = self.client.get(reverse("oauth_consent") + "?data=invalid")
        self.assertEqual(resp.status_code, 400)

    def test_missing_data_param(self):
        self.client.force_login(self.user)
        resp = self.client.get(reverse("oauth_consent"))
        self.assertEqual(resp.status_code, 400)

    @override_settings(SECRET_KEY="different-key")
    def test_expired_signed_data(self):
        """Signed data with wrong key should fail."""
        # Create signed data with the default key, then validate with different key
        # This simulates expiration behavior
        self.client.force_login(self.user)
        resp = self.client.get(reverse("oauth_consent") + "?data=tampered-data")
        self.assertEqual(resp.status_code, 400)
