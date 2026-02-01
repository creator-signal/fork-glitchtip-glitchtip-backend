import hashlib

from django.db import models

from glitchtip.partition_manager import UUID7Helper

from .constants import LogLevel

# Number of hash buckets for service names in stats (0-255)
SERVICE_HASH_BUCKETS = 256


def compute_service_hash(service_name: str) -> int:
    """
    Hash a service name to a bucket (0-255).

    Uses MD5 for fast, uniform distribution. Not cryptographic,
    just needs to be consistent and well-distributed.
    """
    if not service_name:
        return 0
    digest = hashlib.md5(service_name.encode(), usedforsecurity=False).digest()
    return digest[0]  # First byte gives 0-255


class LogEvent(models.Model):
    """
    Log event storage with optimized column alignment.

    Column alignment (reduces padding, improves CPU cache):
    - 16-byte: UUIDs (id, trace_id)
    - 8-byte: ForeignKeys and BigInt (organization, project, span_id)
    - 2-byte: SmallIntegers (level, severity_number)
    - Variable: Text/JSON fields (body, service, data)

    Timestamp is derived from the UUIDv7 id field which encodes millisecond-precise
    client timestamp. No separate timestamp column - the id IS the timestamp.

    Partitioning:
    - Uses native Python PartitionManager
    - Partitioned by RANGE on id (UUIDv7)
    - Sub-partitioned by HASH on organization_id
    """

    # 16-byte alignment: UUIDs
    id = models.UUIDField(
        editable=False,
        help_text="UUIDv7 generated from client timestamp (partition key)",
    )
    # Primary Key is composite (id, organization) to allow HASH sub-partitioning
    pk = models.CompositePrimaryKey("id", "organization")

    trace_id = models.UUIDField(
        null=True,
        blank=True,
        help_text="Trace ID for correlating logs with traces",
    )

    # 8-byte alignment: ForeignKeys and BigInt
    organization = models.ForeignKey(
        "organizations_ext.Organization", on_delete=models.CASCADE
    )
    project = models.ForeignKey("projects.Project", on_delete=models.CASCADE)
    span_id = models.BigIntegerField(
        null=True,
        blank=True,
        help_text="Span ID for correlating logs with specific spans (8-byte)",
    )

    # 2-byte alignment
    level = models.PositiveSmallIntegerField(
        choices=LogLevel.choices,
        default=LogLevel.INFO,
        help_text="Log level (trace/debug/info/warn/error/fatal)",
    )
    severity_number = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text="OpenTelemetry severity number (1-24)",
    )

    # Variable-width fields
    body = models.TextField(
        help_text="The log message body",
    )
    service = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="Service name that emitted the log",
    )
    data = models.JSONField(
        default=dict,
        blank=True,
        help_text="Additional structured data/attributes",
    )

    class Meta:
        indexes = [
            # Primary query pattern: list logs for org, ordered by time
            models.Index(fields=["organization", "-id"], name="logevent_org_id_idx"),
            # Filter by project within org
            models.Index(fields=["project", "-id"], name="logevent_proj_id_idx"),
            # Filter by level
            models.Index(
                fields=["organization", "level", "-id"], name="logevent_org_level_idx"
            ),
            # Trace correlation
            models.Index(
                fields=["trace_id"],
                name="logevent_trace_id_idx",
                condition=models.Q(trace_id__isnull=False),
            ),
        ]

    def __str__(self):
        return f"[{self.get_level_display()}] {self.body[:50]}"

    @property
    def timestamp(self):
        """
        Time at which log was emitted by the application.

        Derived from UUIDv7 id which encodes millisecond-precision timestamp
        from the client's reported event time.
        """
        return UUID7Helper.extract_datetime(self.id)


class LogService(models.Model):
    """
    Lookup table for unique service names per organization.

    This allows the UI to show a dropdown of known services without
    querying the large logs table. Updated during log ingestion.
    """

    organization = models.ForeignKey(
        "organizations_ext.Organization", on_delete=models.CASCADE
    )
    name = models.CharField(max_length=255, help_text="Service name")
    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "name"], name="unique_org_service"
            )
        ]
        indexes = [
            models.Index(fields=["organization"], name="logservice_org_idx"),
        ]

    def __str__(self):
        return self.name
