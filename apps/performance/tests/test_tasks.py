from django.db import models
from django.utils import timezone
from model_bakery import baker

from glitchtip.test_utils.test_case import GlitchTipTestCase

from ..maintenance import cleanup_old_transaction_events
from ..models import TransactionEvent, TransactionGroup


class TasksTestCase(GlitchTipTestCase):
    def test_cleanup_old_events(self):
        groups = baker.make("performance.TransactionGroup", _quantity=2)
        baker.make("performance.TransactionEvent", group=groups[0])
        cleanup_old_transaction_events()
        self.assertEqual(TransactionGroup.objects.count(), 1)

        TransactionEvent.objects.all().delete()
        cleanup_old_transaction_events()
        self.assertEqual(TransactionGroup.objects.count(), 0)

    def test_cleanup_handles_all_fk_relations(self):
        """
        Verify each CASCADE FK to TransactionGroup is filtered in cleanup.

        If a new model with a FK to TransactionGroup is added, this test
        will fail — update cleanup_old_transaction_events() to filter for
        the new relation.
        """
        for rel in TransactionGroup._meta.related_objects:
            if rel.on_delete != models.CASCADE:
                continue
            if rel.related_model._meta.auto_created:
                continue
            accessor = rel.get_accessor_name()
            with self.subTest(relation=accessor):
                group = baker.make("performance.TransactionGroup")
                kwargs = {rel.field.name: group}
                # Set datetime fields to now() for partition compatibility
                for f in rel.related_model._meta.concrete_fields:
                    if (
                        isinstance(f, models.DateTimeField)
                        and not f.has_default()
                        and not f.null
                    ):
                        kwargs[f.name] = timezone.now()
                baker.make(rel.related_model, **kwargs)

                cleanup_old_transaction_events()
                self.assertTrue(
                    TransactionGroup.objects.filter(id=group.id).exists(),
                    f"Group with {accessor} was deleted. Update "
                    f"cleanup_old_transaction_events() to filter "
                    f"for {accessor}=None",
                )
