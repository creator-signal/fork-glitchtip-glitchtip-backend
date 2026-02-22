from django.test import TestCase
from django.utils import timezone

from apps.performance.models import TransactionGroup


class TransactionGroupModelTestCase(TestCase):
    def test_create_group(self):
        from model_bakery import baker

        now = timezone.now()
        project = baker.make("projects.Project")
        group = TransactionGroup.objects.create(
            project=project,
            organization=project.organization,
            transaction="/api/test/",
            op="http.server",
            method="GET",
            first_seen=now,
            last_seen=now,
            avg_duration=150.5,
            count=42,
            error_count=3,
        )
        self.assertEqual(str(group), "/api/test/")
        self.assertEqual(group.count, 42)
        self.assertEqual(group.error_count, 3)
        self.assertAlmostEqual(group.avg_duration, 150.5)

    def test_default_values(self):
        from model_bakery import baker

        now = timezone.now()
        project = baker.make("projects.Project")
        group = TransactionGroup.objects.create(
            project=project,
            organization=project.organization,
            transaction="/test/",
            op="http.server",
            first_seen=now,
            last_seen=now,
        )
        self.assertEqual(group.count, 0)
        self.assertEqual(group.error_count, 0)
        self.assertEqual(group.avg_duration, 0)
        self.assertIsNone(group.p50)
        self.assertIsNone(group.p95)
        self.assertEqual(group.duration_histogram, {})

    def test_unique_constraint(self):
        from django.db import IntegrityError
        from model_bakery import baker

        now = timezone.now()
        project = baker.make("projects.Project")
        TransactionGroup.objects.create(
            project=project,
            organization=project.organization,
            transaction="/api/test/",
            op="http.server",
            method="GET",
            first_seen=now,
            last_seen=now,
        )
        with self.assertRaises(IntegrityError):
            TransactionGroup.objects.create(
                project=project,
                organization=project.organization,
                transaction="/api/test/",
                op="http.server",
                method="GET",
                first_seen=now,
                last_seen=now,
            )

    def test_histogram_json_field(self):
        from model_bakery import baker

        now = timezone.now()
        project = baker.make("projects.Project")
        group = TransactionGroup.objects.create(
            project=project,
            organization=project.organization,
            transaction="/test/",
            op="http.server",
            first_seen=now,
            last_seen=now,
            duration_histogram={"0": 5, "10": 3, "20": 1},
        )
        group.refresh_from_db()
        self.assertEqual(group.duration_histogram["0"], 5)
        self.assertEqual(group.duration_histogram["10"], 3)
