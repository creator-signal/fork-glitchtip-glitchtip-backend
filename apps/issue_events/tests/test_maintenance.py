from datetime import timedelta

from django.conf import settings
from django.db import models
from django.test import TestCase
from django.utils.timezone import now
from freezegun import freeze_time
from model_bakery import baker

from ..maintenance import cleanup_old_issues
from ..models import Issue, IssueEvent


class MaintenanceTestCase(TestCase):
    def test_cleanup_old_issues(self):
        events = baker.make(
            "issue_events.IssueEvent", _quantity=5, _fill_optional=["issue"]
        )
        baker.make("issue_events.IssueEvent", issue=events[0].issue, _quantity=5)
        cleanup_old_issues()
        self.assertEqual(Issue.objects.count(), 5)

        IssueEvent.objects.all().delete()
        with freeze_time(
            now() + timedelta(days=settings.GLITCHTIP_MAX_EVENT_LIFE_DAYS)
        ):
            cleanup_old_issues()
            self.assertEqual(Issue.objects.count(), 0)

    def test_cleanup_handles_all_fk_relations(self):
        """
        Verify each CASCADE FK to Issue is filtered in cleanup.

        If a new model with a FK to Issue is added, this test will fail —
        update cleanup_old_issues() to filter for the new relation.
        """
        with freeze_time(
            now() + timedelta(days=settings.GLITCHTIP_MAX_EVENT_LIFE_DAYS)
        ):
            for rel in Issue._meta.related_objects:
                if rel.on_delete != models.CASCADE:
                    continue
                # Skip auto-generated M2M through tables
                if rel.related_model._meta.auto_created:
                    continue
                accessor = rel.get_accessor_name()
                with self.subTest(relation=accessor):
                    issue = baker.make("issue_events.Issue")
                    kwargs = {rel.field.name: issue}
                    # Set datetime fields to now() for partition compatibility
                    for f in rel.related_model._meta.concrete_fields:
                        if (
                            isinstance(f, models.DateTimeField)
                            and not f.has_default()
                            and not f.null
                        ):
                            kwargs[f.name] = now()
                    baker.make(rel.related_model, **kwargs)

                    cleanup_old_issues()
                    self.assertTrue(
                        Issue.objects.filter(id=issue.id).exists(),
                        f"Issue with {accessor} was deleted. Update "
                        f"cleanup_old_issues() to filter for "
                        f"{accessor}=None",
                    )
