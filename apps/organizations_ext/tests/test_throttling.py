from datetime import datetime, timedelta

from django.test import TestCase, override_settings
from django.utils import timezone
from freezegun import freeze_time
from model_bakery import baker

from ..models import Organization
from ..tasks import (
    check_all_organizations_throttle,
    check_organization_throttle,
    get_free_tier_organizations_with_event_count,
)


class OrganizationThrottleCheckTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.product = baker.make(
            "djstripe.Product", active=True, metadata={"events": 10}
        )
        cls.plan = baker.make(
            "djstripe.Plan", active=True, amount=0, product=cls.product
        )
        cls.organization = baker.make("organizations_ext.Organization")
        cls.user = baker.make("users.user")
        cls.organization.add_user(cls.user)
        cls.customer = baker.make(
            "djstripe.Customer", subscriber=cls.organization, livemode=False
        )
        cls.subscription = baker.make(
            "djstripe.Subscription",
            customer=cls.customer,
            livemode=False,
            plan=cls.plan,
            status="active",
            current_period_end=timezone.now() + timedelta(hours=1),
        )

    def _make_events(self, i: int):
        baker.make(
            "projects.IssueEventProjectHourlyStatistic",
            project__organization=self.organization,
            count=i,
        )

    def _make_transaction_events(self, i: int):
        baker.make(
            "projects.TransactionEventProjectHourlyStatistic",
            project__organization=self.organization,
            count=i,
        )

    @override_settings(
        CACHES={"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}}
    )
    def test_check_organization_throttle(self):
        check_organization_throttle(self.organization.id)
        self.assertTrue(Organization.objects.filter(event_throttle_rate=0).exists())

        baker.make(
            "projects.IssueEventProjectHourlyStatistic",
            project__organization=self.organization,
            count=11,
        )
        check_organization_throttle(self.organization.id)
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.event_throttle_rate, 10)

        baker.make(
            "projects.IssueEventProjectHourlyStatistic",
            project__organization=self.organization,
            count=100,
        )
        check_organization_throttle(self.organization.id)
        self.organization.refresh_from_db()
        self.assertEqual(self.organization.event_throttle_rate, 100)

    def test_check_all_organizations_throttle(self):
        org = self.organization

        # No events, no throttle
        check_all_organizations_throttle()
        org.refresh_from_db()
        self.assertEqual(org.event_throttle_rate, 0)

        # 6 events (of 10), no throttle
        self._make_events(3)
        self._make_transaction_events(3)
        check_all_organizations_throttle()
        org.refresh_from_db()
        self.assertEqual(org.event_throttle_rate, 0)

        # 11 events (of 10), small throttle
        self._make_events(5)
        check_all_organizations_throttle()
        org.refresh_from_db()
        self.assertEqual(org.event_throttle_rate, 10)

        # New time period, should reset throttle
        now = self.subscription.current_period_start + timedelta(hours=1)
        self.subscription.current_period_start = now
        self.subscription.current_period_end = now + timedelta(hours=1)
        self.subscription.save()
        check_all_organizations_throttle()
        org.refresh_from_db()
        self.assertEqual(org.event_throttle_rate, 0)

        # Throttle again
        with freeze_time(now):
            self._make_events(16)
        check_all_organizations_throttle()
        org.refresh_from_db()
        self.assertEqual(org.event_throttle_rate, 50)

        # Throttle 100%
        with freeze_time(now):
            self._make_events(5)
        check_all_organizations_throttle()
        org.refresh_from_db()
        self.assertEqual(org.event_throttle_rate, 100)


class OrganizationThrottlingTestCase(TestCase):
    def test_organization_event_count(self):
        plan = baker.make("djstripe.Plan", active=True, amount=0)
        organization = baker.make("organizations_ext.Organization")
        project = baker.make("projects.Project", organization=organization)
        user = baker.make("users.user")
        organization.add_user(user)
        customer = baker.make(
            "djstripe.Customer", subscriber=organization, livemode=False
        )

        with freeze_time(datetime(2000, 1, 1)):
            baker.make(
                "djstripe.Subscription",
                customer=customer,
                livemode=False,
                plan=plan,
                status="active",
                current_period_end=timezone.make_aware(datetime(2000, 2, 1)),
            )
            baker.make("issue_events.IssueEvent", issue__project=project, _quantity=3)
            baker.make(
                "projects.IssueEventProjectHourlyStatistic", project=project, count=3
            )
            baker.make(
                "performance.TransactionEvent",
                group__project=project,
                _quantity=2,
            )
            baker.make(
                "projects.TransactionEventProjectHourlyStatistic",
                project=project,
                count=2,
            )
            free_org = get_free_tier_organizations_with_event_count().first()
        self.assertEqual(free_org.total_event_count, 5)

    @override_settings(BILLING_FREE_TIER_EVENTS=1)
    def test_non_subscriber_throttling_performance(self):
        for _ in range(2):
            plan = baker.make("djstripe.Plan", active=True, amount=0)
            organization = baker.make("organizations_ext.Organization")
            user = baker.make("users.user")
            organization.add_user(user)
            customer = baker.make(
                "djstripe.Customer", subscriber=organization, livemode=False
            )
            baker.make(
                "djstripe.Subscription",
                customer=customer,
                livemode=False,
                plan=plan,
                status="active",
            )
            baker.make(
                "issue_events.IssueEvent",
                issue__project__organization=organization,
                _quantity=2,
            )
            baker.make(
                "projects.IssueEventProjectHourlyStatistic",
                project__organization=organization,
                count=2,
            )
        with self.assertNumQueries(4):
            check_all_organizations_throttle()

    @override_settings(BILLING_FREE_TIER_EVENTS=1)
    def test_no_plan_throttle(self):
        """
        It's possible to not sign up for a free plan, they should be limited to free tier events
        """
        organization = baker.make("organizations_ext.Organization")
        user = baker.make("users.user")
        organization.add_user(user)
        project = baker.make("projects.Project", organization=organization)
        baker.make("issue_events.IssueEvent", issue__project=project, _quantity=2)
        check_all_organizations_throttle()
        organization.refresh_from_db()
        self.assertFalse(organization.is_accepting_events)

        # Make plan active
        customer = baker.make(
            "djstripe.Customer", subscriber=organization, livemode=False
        )
        plan = baker.make("djstripe.Plan", active=True, amount=1)
        subscription = baker.make(
            "djstripe.Subscription",
            customer=customer,
            livemode=False,
            plan=plan,
            status="active",
            current_period_end=timezone.make_aware(datetime(2000, 1, 31)),
        )
        check_all_organizations_throttle()
        organization.refresh_from_db()
        self.assertTrue(organization.is_accepting_events)

        # Cancel plan
        subscription.status = "canceled"
        subscription.save()
        check_all_organizations_throttle()
        organization.refresh_from_db()
        self.assertFalse(organization.is_accepting_events)

        # Add new active plan (still has canceled plan)
        subscription = baker.make(
            "djstripe.Subscription",
            customer=customer,
            livemode=False,
            plan=plan,
            status="active",
            current_period_end=timezone.make_aware(datetime(2000, 1, 31)),
        )
        check_all_organizations_throttle()
        organization.refresh_from_db()
        self.assertTrue(organization.is_accepting_events)

    def test_canceled_plan(self):
        # Start with no plan and throttled
        organization = baker.make(
            "organizations_ext.Organization", is_accepting_events=False
        )
        user = baker.make("users.user")
        organization.add_user(user)
        organization.refresh_from_db()
        self.assertFalse(organization.is_accepting_events)

        # Add old paid plan and active free plan
        customer = baker.make(
            "djstripe.Customer", subscriber=organization, livemode=False
        )
        free_plan = baker.make("djstripe.Plan", active=True, amount=0)
        paid_plan = baker.make("djstripe.Plan", active=True, amount=1)
        baker.make(
            "djstripe.Subscription",
            customer=customer,
            livemode=False,
            plan=paid_plan,
            status="canceled",
            current_period_end=timezone.make_aware(datetime(2000, 1, 31)),
        )
        baker.make(
            "djstripe.Subscription",
            customer=customer,
            livemode=False,
            plan=free_plan,
            status="active",
            current_period_end=timezone.make_aware(datetime(2100, 1, 31)),
        )

        # Should not be throttled
        check_all_organizations_throttle()
        organization.refresh_from_db()
        self.assertTrue(organization.is_accepting_events)
