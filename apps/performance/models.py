from django.db import models

from glitchtip.base_models import CreatedModel
from glitchtip.partition_manager import UUID7Helper


def _generate_uuid7():
    """Generate UUIDv7. Kept for old migration compatibility."""
    return UUID7Helper.from_datetime()


class TransactionGroup(CreatedModel):
    # 8-byte alignment: FKs
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE)
    organization = models.ForeignKey(
        "organizations_ext.Organization", on_delete=models.CASCADE
    )

    # Variable-width fields
    transaction = models.CharField(max_length=1024)
    op = models.CharField(max_length=255)
    method = models.CharField(max_length=255, blank=True)

    # 8-byte alignment: timestamps and floats
    first_seen = models.DateTimeField()
    last_seen = models.DateTimeField()
    avg_duration = models.FloatField(default=0, help_text="Average duration in ms")
    p50 = models.FloatField(null=True, blank=True)
    p95 = models.FloatField(null=True, blank=True)

    # 4-byte alignment: integers
    count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)

    # Variable-width
    duration_histogram = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["transaction", "project", "op", "method"],
                name="unique_transaction_project_op_method",
            )
        ]
        indexes = [
            models.Index(
                fields=["organization", "last_seen"],
                name="perf_txgroup_org_lastseen",
            ),
        ]

    def __str__(self):
        return self.transaction


class SpanStaging(models.Model):
    """
    Write-heavy staging table for span data before promotion to Parquet.

    managed = False — created via raw SQL.
    Partitioned by RANGE on id (UUIDv7) with HASH sub-partitioning on
    organization_id, matching the IssueEvent pattern.
    No additional indexes — optimized for bulk inserts.
    """

    id = models.UUIDField(default=_generate_uuid7, editable=False)
    pk = models.CompositePrimaryKey("id", "organization")
    organization = models.ForeignKey(
        "organizations_ext.Organization", on_delete=models.DO_NOTHING
    )
    project_id = models.IntegerField()
    transaction_name = models.CharField(max_length=1024)
    span_id = models.CharField(max_length=32)
    transaction_id = models.CharField(max_length=32)
    op = models.CharField(max_length=255)
    description = models.CharField(max_length=500, blank=True)
    duration = models.FloatField(help_text="Duration in milliseconds")
    timestamp = models.DateTimeField(help_text="Span start time")

    class Meta:
        managed = False
        db_table = "performance_spanstaging"
