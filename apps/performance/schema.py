from ninja import Field, ModelSchema, Schema

from glitchtip.schema import CamelSchema

from .models import TransactionGroup


class TransactionGroupSchema(CamelSchema, ModelSchema):
    project: int = Field(validation_alias="project_id")

    class Meta:
        model = TransactionGroup
        fields = [
            "id",
            "transaction",
            "op",
            "method",
            "count",
            "avg_duration",
            "p50",
            "p95",
            "error_count",
            "first_seen",
            "last_seen",
        ]


class SpanGroupSchema(CamelSchema, Schema):
    op: str
    description: str
    count: int
    avg_duration: float
    p95_duration: float
    total_time: float


class SlowQuerySchema(CamelSchema, Schema):
    op: str
    description: str
    count: int
    avg_duration: float
    total_time: float
