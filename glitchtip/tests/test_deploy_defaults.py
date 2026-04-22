"""Tests for settings auto-derivation and production-default warnings.

These settings choices run at module import time, so each case spawns a fresh
Python process with targeted env vars and asserts the resulting `settings`.
"""

import os
import subprocess
import sys

from django.test import SimpleTestCase


def _load_settings(env_overrides: dict[str, str]) -> subprocess.CompletedProcess:
    """Run a subprocess that imports Django settings under the given env and
    emits a short report on stdout (settings values) and stderr (warnings).
    """
    code = (
        "import django; django.setup();"
        "from django.conf import settings;"
        "print('SESSION_COOKIE_SECURE', settings.SESSION_COOKIE_SECURE);"
        "print('CSRF_COOKIE_SECURE', settings.CSRF_COOKIE_SECURE);"
        "print('SECRET_KEY_IS_DEFAULT', settings.SECRET_KEY == 'change_me');"
        "print('ALLOWED_HOSTS', settings.ALLOWED_HOSTS);"
    )
    env = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "glitchtip.settings",
        # Minimum viable env. Subclasses override via env_overrides.
        "DEBUG": "false",
        "SECRET_KEY": "test-secret-key-not-default",
        "DATABASE_URL": "postgres://postgres:postgres@localhost:5432/postgres",
        "GLITCHTIP_URL": "https://example.com",
        "ALLOWED_HOSTS": "example.com",
        **env_overrides,
    }
    return subprocess.run(
        [sys.executable, "-W", "always", "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _parse(stdout: str) -> dict[str, str]:
    return dict(line.split(" ", 1) for line in stdout.strip().splitlines() if line)


class CookieSecureDefaultsTests(SimpleTestCase):
    def test_https_url_defaults_cookies_to_secure(self):
        r = _load_settings({"GLITCHTIP_URL": "https://example.com"})
        self.assertEqual(r.returncode, 0, r.stderr)
        parsed = _parse(r.stdout)
        self.assertEqual(parsed["SESSION_COOKIE_SECURE"], "True")
        self.assertEqual(parsed["CSRF_COOKIE_SECURE"], "True")

    def test_http_url_defaults_cookies_to_insecure(self):
        r = _load_settings({"GLITCHTIP_URL": "http://localhost:8000"})
        self.assertEqual(r.returncode, 0, r.stderr)
        parsed = _parse(r.stdout)
        self.assertEqual(parsed["SESSION_COOKIE_SECURE"], "False")
        self.assertEqual(parsed["CSRF_COOKIE_SECURE"], "False")

    def test_env_override_wins_over_derived_default(self):
        r = _load_settings(
            {
                "GLITCHTIP_URL": "https://example.com",
                "SESSION_COOKIE_SECURE": "false",
                "CSRF_COOKIE_SECURE": "false",
            }
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        parsed = _parse(r.stdout)
        self.assertEqual(parsed["SESSION_COOKIE_SECURE"], "False")
        self.assertEqual(parsed["CSRF_COOKIE_SECURE"], "False")


class ProductionWarningTests(SimpleTestCase):
    def test_default_secret_key_warns_in_production(self):
        r = _load_settings({"SECRET_KEY": "change_me", "DEBUG": "false"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("SECRET_KEY is still the placeholder default", r.stderr)

    def test_default_secret_key_silent_in_debug(self):
        r = _load_settings({"SECRET_KEY": "change_me", "DEBUG": "true"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("SECRET_KEY is still the placeholder default", r.stderr)

    def test_real_secret_key_silent(self):
        r = _load_settings({"SECRET_KEY": "real-secret-abc123xyz789", "DEBUG": "false"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("SECRET_KEY is still the placeholder default", r.stderr)

    def test_wildcard_allowed_hosts_warns_in_production(self):
        r = _load_settings({"ALLOWED_HOSTS": "*", "DEBUG": "false"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ALLOWED_HOSTS is the wildcard default", r.stderr)

    def test_wildcard_allowed_hosts_silent_in_debug(self):
        r = _load_settings({"ALLOWED_HOSTS": "*", "DEBUG": "true"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("ALLOWED_HOSTS is the wildcard default", r.stderr)

    def test_scoped_allowed_hosts_silent(self):
        r = _load_settings({"ALLOWED_HOSTS": "glitchtip.example.com", "DEBUG": "false"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("ALLOWED_HOSTS is the wildcard default", r.stderr)
