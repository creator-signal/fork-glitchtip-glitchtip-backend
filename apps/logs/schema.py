from datetime import datetime
from uuid import UUID

from ninja import Field, Schema

from glitchtip.schema import CamelSchema

from .constants import LogLevel


class LogEventSchema(CamelSchema):
    """Schema for log event API responses."""

    id: UUID
    timestamp: datetime
    level: str
    body: str
    service: str
    trace_id: UUID | None = Field(None, alias="traceID")
    span_id: str | None = Field(None, alias="spanID")
    severity_number: int | None = None
    data: dict = Field(default_factory=dict)
    project_id: int = Field(alias="projectId")

    @staticmethod
    def resolve_level(obj) -> str:
        """Convert level integer to string."""
        return LogLevel(obj.level).label

    @staticmethod
    def resolve_timestamp(obj) -> datetime:
        """Get timestamp from UUIDv7 id (client event time)."""
        return obj.timestamp

    @staticmethod
    def resolve_span_id(obj) -> str | None:
        """Convert span_id integer to hex string for display."""
        if obj.span_id is None:
            return None
        # Format as 16-character hex string (8 bytes)
        return f"{obj.span_id:016x}"


class LogFilterSchema(Schema):
    """Schema for log filtering parameters."""

    project: list[int] | None = Field(default=None, description="Filter by project IDs")
    level: list[str] | None = Field(default=None, description="Filter by log levels")
    service: str | None = Field(default=None, description="Filter by service name")
    trace_id: str | None = Field(
        default=None, alias="traceId", description="Filter by trace ID"
    )
    query: str | None = Field(default=None, description="Search in log body")
    start: datetime | None = Field(default=None, description="Start of time range")
    end: datetime | None = Field(default=None, description="End of time range")
    cursor: str | None = Field(default=None, description="Pagination cursor")
    limit: int = Field(default=100, ge=1, le=200, description="Results per page")


class LogStatsFilterSchema(Schema):
    """Schema for log stats filtering parameters."""

    project: list[int] | None = Field(default=None, description="Filter by project IDs")
    level: list[str] | None = Field(default=None, description="Filter by log levels")
    service: list[str] | None = Field(
        default=None, description="Filter by service names"
    )
    start: datetime | None = Field(default=None, description="Start of time range")
    end: datetime | None = Field(default=None, description="End of time range")


class LogStatsSeriesSchema(CamelSchema):
    """A single series in the stats response."""

    name: str
    data: list[int]


class LogStatsSchema(CamelSchema):
    """Schema for log stats response."""

    intervals: list[datetime]
    series: list[LogStatsSeriesSchema]


class LogServiceSchema(CamelSchema):
    """Schema for service name in list."""

    name: str
    last_seen: datetime
