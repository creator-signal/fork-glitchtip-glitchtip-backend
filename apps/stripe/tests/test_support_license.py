from django.test import TestCase, override_settings

from apps.stripe.models import SupportLicense


class SupportLicenseSingletonTestCase(TestCase):
    def test_save_forces_pk_one(self):
        sl = SupportLicense(license_key="sub_first")
        sl.save()
        self.assertEqual(sl.pk, 1)

        sl2 = SupportLicense(license_key="sub_second")
        sl2.save()
        self.assertEqual(sl2.pk, 1)
        self.assertEqual(SupportLicense.objects.count(), 1)

        # The second save overwrote the first row.
        self.assertEqual(SupportLicense.objects.get().license_key, "sub_second")

    def test_load_creates_when_missing(self):
        self.assertEqual(SupportLicense.objects.count(), 0)
        sl = SupportLicense.load()
        self.assertEqual(sl.pk, 1)
        self.assertEqual(sl.license_key, "")
        self.assertEqual(SupportLicense.objects.count(), 1)

    def test_load_returns_existing(self):
        SupportLicense(license_key="sub_existing", billing_email="a@b.co").save()
        sl = SupportLicense.load()
        self.assertEqual(sl.license_key, "sub_existing")
        self.assertEqual(sl.billing_email, "a@b.co")


class SupportLicenseResolverTestCase(TestCase):
    @override_settings(
        BILLING_ENABLED=True,
        GLITCHTIP_LICENSE_KEY="sub_envKey",
        GLITCHTIP_BILLING_EMAIL="env@example.com",
    )
    def test_billing_enabled_short_circuits_to_empty(self):
        SupportLicense(license_key="sub_dbKey", billing_email="db@example.com").save()
        self.assertEqual(SupportLicense.resolved(), ("", ""))

    @override_settings(
        BILLING_ENABLED=False,
        GLITCHTIP_LICENSE_KEY="sub_envKey",
        GLITCHTIP_BILLING_EMAIL="env@example.com",
    )
    def test_env_wins_when_both_env_vars_set(self):
        SupportLicense(license_key="sub_dbKey", billing_email="db@example.com").save()
        self.assertEqual(
            SupportLicense.resolved(), ("sub_envKey", "env@example.com")
        )

    @override_settings(
        BILLING_ENABLED=False,
        GLITCHTIP_LICENSE_KEY=None,
        GLITCHTIP_BILLING_EMAIL=None,
    )
    def test_db_used_when_no_env(self):
        SupportLicense(license_key="sub_dbKey", billing_email="db@example.com").save()
        self.assertEqual(SupportLicense.resolved(), ("sub_dbKey", "db@example.com"))

    @override_settings(
        BILLING_ENABLED=False,
        GLITCHTIP_LICENSE_KEY="sub_envKey",
        GLITCHTIP_BILLING_EMAIL=None,
    )
    def test_partial_env_uses_db_for_missing_field(self):
        SupportLicense(license_key="sub_dbKey", billing_email="db@example.com").save()
        self.assertEqual(
            SupportLicense.resolved(), ("sub_envKey", "db@example.com")
        )

    @override_settings(
        BILLING_ENABLED=False,
        GLITCHTIP_LICENSE_KEY=None,
        GLITCHTIP_BILLING_EMAIL=None,
    )
    def test_empty_db_returns_empty(self):
        self.assertEqual(SupportLicense.resolved(), ("", ""))

    @override_settings(
        BILLING_ENABLED=False,
        GLITCHTIP_LICENSE_KEY="",
        GLITCHTIP_BILLING_EMAIL="",
    )
    def test_empty_string_env_does_not_fall_through_to_db(self):
        SupportLicense(license_key="sub_dbKey", billing_email="db@example.com").save()
        self.assertEqual(SupportLicense.resolved(), ("", ""))
