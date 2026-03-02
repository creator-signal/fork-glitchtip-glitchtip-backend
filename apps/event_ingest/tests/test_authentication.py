import gzip
import json
import uuid

from django.core.cache import cache
from django.tasks import task_backends
from django.test import TestCase
from django.urls import reverse
from django.urls.exceptions import NoReverseMatch
from model_bakery import baker

from apps.event_ingest.views import TUNNEL_AUTH_MAX_BYTES
from apps.issue_events.models import IssueEvent


class AuthenticationTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = baker.make("users.User")
        cls.project = baker.make("projects.Project")
        cls.project_key = cls.project.projectkey_set.first()
        cls.organization = cls.project.organization
        # Add user to organization to create OrganizationOwner (billing contact)
        cls.organization.add_user(cls.user)

    def setUp(self):
        cache.clear()
        self.url = (
            reverse("event_envelope", args=[self.project.id])
            + f"?sentry_key={self.project_key.public_key}"
        )

    def test_org_throttle(self):
        res = self.client.post(self.url, [{}], content_type="application/json")
        self.assertEqual(res.status_code, 200)
        self.organization.event_throttle_rate = 100
        self.organization.save()
        res = self.client.post(self.url, [{}], content_type="application/json")
        self.assertEqual(res.headers.get("Retry-After"), "600")
        self.assertEqual(res.status_code, 429)

    def test_invalid_project_id(self):
        with self.assertRaises(NoReverseMatch):
            reverse("event_envelope", args=[f"{self.project.id}''"])


class TunnelAuthTestCase(TestCase):
    """
    Test JS tunnel support: DSN extracted from envelope body when not in headers.
    Simulates browser -> tunnel proxy -> GlitchTip where the proxy strips auth
    headers and the DSN only exists in the envelope header line.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user = baker.make("users.User")
        cls.project = baker.make("projects.Project")
        cls.project_key = cls.project.projectkey_set.first()
        cls.organization = cls.project.organization
        cls.organization.add_user(cls.user)

    def setUp(self):
        cache.clear()
        # URL without sentry_key query param — simulates tunnel
        self.url = reverse("event_envelope", args=[self.project.id])
        self.dsn = self.project_key.get_dsn()

    def _make_envelope(self, envelope_header, items=None):
        """Build a newline-delimited envelope string."""
        lines = [json.dumps(envelope_header)]
        if items is None:
            # Default: one error event item
            lines.append(json.dumps({"type": "event"}))
            lines.append(json.dumps({"exception": {"values": []}}))
        else:
            for item in items:
                lines.append(json.dumps(item))
        return "\n".join(lines)

    def test_tunnel_auth_happy_path(self):
        """Valid DSN in envelope body, no auth headers — should succeed."""
        envelope = self._make_envelope({"event_id": uuid.uuid4().hex, "dsn": self.dsn})
        res = self.client.post(self.url, envelope, content_type="application/json")
        task_backends["default"].flush_batches()
        self.assertEqual(res.status_code, 200)
        self.assertTrue(IssueEvent.objects.exists())

    def test_tunnel_auth_with_gzip(self):
        """Valid tunneled request with gzip compression."""
        envelope = self._make_envelope({"event_id": uuid.uuid4().hex, "dsn": self.dsn})
        compressed = gzip.compress(envelope.encode())
        res = self.client.post(
            self.url,
            compressed,
            content_type="application/octet-stream",
            HTTP_CONTENT_ENCODING="gzip",
        )
        task_backends["default"].flush_batches()
        self.assertEqual(res.status_code, 200)
        self.assertTrue(IssueEvent.objects.exists())

    def test_tunnel_auth_invalid_dsn(self):
        """Invalid DSN in envelope body — should 403."""
        envelope = self._make_envelope(
            {
                "event_id": uuid.uuid4().hex,
                "dsn": "https://00000000000000000000000000000000@localhost/999",
            }
        )
        res = self.client.post(self.url, envelope, content_type="application/json")
        self.assertEqual(res.status_code, 403)
        self.assertFalse(IssueEvent.objects.exists())

    def test_tunnel_auth_no_dsn_in_body(self):
        """Envelope header without DSN field — should 403."""
        envelope = self._make_envelope({"event_id": uuid.uuid4().hex})
        res = self.client.post(self.url, envelope, content_type="application/json")
        self.assertEqual(res.status_code, 403)

    def test_tunnel_auth_empty_body(self):
        """Empty POST body without auth headers — should 403."""
        res = self.client.post(self.url, b"", content_type="application/octet-stream")
        self.assertEqual(res.status_code, 403)

    def test_tunnel_auth_garbage_body(self):
        """Random bytes without auth headers — should 403."""
        res = self.client.post(
            self.url, b"\x00\xff" * 100, content_type="application/octet-stream"
        )
        self.assertEqual(res.status_code, 403)

    def test_header_auth_still_works(self):
        """Normal header-based auth is unaffected by tunnel code."""
        url_with_key = self.url + f"?sentry_key={self.project_key.public_key}"
        envelope = self._make_envelope({"event_id": uuid.uuid4().hex})
        res = self.client.post(url_with_key, envelope, content_type="application/json")
        task_backends["default"].flush_batches()
        self.assertEqual(res.status_code, 200)
        self.assertTrue(IssueEvent.objects.exists())

    def test_tunnel_auth_throttled_project(self):
        """Throttled project via tunnel should return 429."""
        self.organization.event_throttle_rate = 100
        self.organization.save()
        envelope = self._make_envelope({"event_id": uuid.uuid4().hex, "dsn": self.dsn})
        res = self.client.post(self.url, envelope, content_type="application/json")
        self.assertEqual(res.status_code, 429)
        self.assertTrue(res.has_header("Retry-After"))

    def test_tunnel_auth_dsn_project_id_ignored(self):
        """URL project_id determines the project, not the project_id in the DSN path.

        This matches Sentry's behavior: the DSN is only used to extract the
        sentry_key, and the URL determines which project receives the event.
        """
        wrong_project_id = self.project.id + 9999
        forged_dsn = (
            f"http://{self.project_key.public_key_hex}@localhost/{wrong_project_id}"
        )
        envelope = self._make_envelope(
            {"event_id": uuid.uuid4().hex, "dsn": forged_dsn}
        )
        res = self.client.post(
            self.url, envelope.encode(), content_type="application/octet-stream"
        )
        task_backends["default"].flush_batches()
        self.assertEqual(res.status_code, 200)


class TunnelAuthDOSTestCase(TestCase):
    """
    DOS resistance tests for the tunnel auth fallback.
    Verify that bounded reads protect against resource exhaustion.
    """

    @classmethod
    def setUpTestData(cls):
        cls.project = baker.make("projects.Project")
        cls.project_key = cls.project.projectkey_set.first()
        cls.organization = cls.project.organization
        cls.user = baker.make("users.User")
        cls.organization.add_user(cls.user)

    def setUp(self):
        cache.clear()
        self.url = reverse("event_envelope", args=[self.project.id])

    def test_large_first_line_no_newline(self):
        """
        Attacker sends a huge first line with no newline.
        Only TUNNEL_AUTH_MAX_BYTES should be read; request rejected quickly.
        """
        payload = b"A" * (1024 * 1024)
        res = self.client.post(
            self.url, payload, content_type="application/octet-stream"
        )
        self.assertEqual(res.status_code, 403)

    def test_large_first_line_with_valid_dsn_after_limit(self):
        """
        Valid DSN exists in the body but only after TUNNEL_AUTH_MAX_BYTES.
        Should not be found — bounded read protects against scanning large payloads.
        """
        dsn = self.project_key.get_dsn()
        padding = b"X" * (TUNNEL_AUTH_MAX_BYTES + 100)
        valid_header = json.dumps({"event_id": uuid.uuid4().hex, "dsn": dsn}).encode()
        payload = padding + b"\n" + valid_header + b"\n"
        res = self.client.post(
            self.url, payload, content_type="application/octet-stream"
        )
        self.assertEqual(res.status_code, 403)

    def test_gzip_bomb_no_auth_headers(self):
        """
        Gzip bomb without auth headers.  The bounded read should only decompress
        ~TUNNEL_AUTH_MAX_BYTES before rejecting (no valid envelope header).
        """
        inner = b"\x00" * (10 * 1024 * 1024)
        compressed = gzip.compress(inner)
        res = self.client.post(
            self.url,
            compressed,
            content_type="application/octet-stream",
            HTTP_CONTENT_ENCODING="gzip",
        )
        self.assertEqual(res.status_code, 403)

    def test_header_within_read_limit(self):
        """Envelope header that fits within TUNNEL_AUTH_MAX_BYTES is parsed."""
        dsn = self.project_key.get_dsn()
        envelope_header = json.dumps({"event_id": uuid.uuid4().hex, "dsn": dsn})
        # Sanity check: header must fit within the bounded read
        self.assertLess(len(envelope_header.encode()), TUNNEL_AUTH_MAX_BYTES)
        payload = (
            envelope_header.encode()
            + b"\n"
            + json.dumps({"type": "event"}).encode()
            + b"\n"
        )
        res = self.client.post(
            self.url, payload, content_type="application/octet-stream"
        )
        task_backends["default"].flush_batches()
        self.assertEqual(res.status_code, 200)

    def test_malformed_dsn_url(self):
        """Malformed DSN URL in envelope header — should 403 without crash."""
        envelope = json.dumps({"event_id": uuid.uuid4().hex, "dsn": "not-a-valid-url"})
        payload = envelope.encode() + b"\n"
        res = self.client.post(
            self.url, payload, content_type="application/octet-stream"
        )
        self.assertEqual(res.status_code, 403)
