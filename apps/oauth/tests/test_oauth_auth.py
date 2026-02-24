"""Test that OAuth access tokens (stored in cache) authenticate API requests."""

import json
import time

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from model_bakery import baker

from apps.api_tokens.models import generate_token


class OAuthTokenAPIAuthTest(TestCase):
    """Test OAuth token authentication on /api/0/ endpoints."""

    def setUp(self):
        self.user = baker.make("users.user")
        self.organization = baker.make("organizations_ext.Organization")
        self.organization.add_user(self.user)
        self.list_url = reverse("api:list_organizations")

    def _store_oauth_token(self, token, scopes, user_id=None, expires_at=None):
        """Helper to store an OAuth access token in cache."""
        if user_id is None:
            user_id = self.user.id
        if expires_at is None:
            expires_at = int(time.time()) + 3600
        data = json.dumps(
            {
                "user_id": user_id,
                "client_id": "test-client",
                "scopes": scopes,
                "expires_at": expires_at,
            }
        )
        cache.set(f"oauth_access:{token}", data, 3600)

    def test_oauth_token_authenticates(self):
        """An OAuth access token in cache should authenticate API requests."""
        token = generate_token()
        self._store_oauth_token(token, ["org:read"])
        res = self.client.get(
            self.list_url, HTTP_AUTHORIZATION=f"Bearer {token}"
        )
        self.assertEqual(res.status_code, 200)

    def test_expired_oauth_token_rejected(self):
        """An expired OAuth access token should be rejected."""
        token = generate_token()
        self._store_oauth_token(token, ["org:read"], expires_at=int(time.time()) - 10)
        res = self.client.get(
            self.list_url, HTTP_AUTHORIZATION=f"Bearer {token}"
        )
        self.assertEqual(res.status_code, 401)

    def test_existing_api_token_still_works(self):
        """Regular APIToken auth should still work."""
        api_token = baker.make("api_tokens.APIToken", user=self.user)
        api_token.add_permission("org:read")
        res = self.client.get(
            self.list_url, HTTP_AUTHORIZATION=f"Bearer {api_token.token}"
        )
        self.assertEqual(res.status_code, 200)

    def test_oauth_scope_enforcement(self):
        """OAuth tokens without required scopes should get 403."""
        token = generate_token()
        # Token has event:read but endpoint requires org:read
        self._store_oauth_token(token, ["event:read"])
        res = self.client.get(
            self.list_url, HTTP_AUTHORIZATION=f"Bearer {token}"
        )
        self.assertEqual(res.status_code, 403)

    def test_oauth_scope_granted(self):
        """OAuth tokens with the right scope should get 200."""
        token = generate_token()
        self._store_oauth_token(token, ["org:read"])
        res = self.client.get(
            self.list_url, HTTP_AUTHORIZATION=f"Bearer {token}"
        )
        self.assertEqual(res.status_code, 200)

    def test_invalid_token_rejected(self):
        """A random token not in DB or cache should be rejected."""
        res = self.client.get(
            self.list_url, HTTP_AUTHORIZATION="Bearer nonexistent_token_xyz"
        )
        self.assertEqual(res.status_code, 401)
