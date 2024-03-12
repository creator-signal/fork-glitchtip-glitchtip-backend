from dateutil.relativedelta import relativedelta
from django.conf import settings

from apps.issue_events.models import IssueEvent, IssueTag
from apps.performance_events import TransactionGroup
from psqlextra.partitioning import (
    PostgresCurrentTimePartitioningStrategy,
    PostgresPartitioningManager,
    PostgresTimePartitionSize,
)
from psqlextra.partitioning.config import PostgresPartitioningConfig

issue_strategy = PostgresCurrentTimePartitioningStrategy(
    size=PostgresTimePartitionSize(days=1),
    count=7,
    max_age=relativedelta(days=settings.GLITCHTIP_MAX_EVENT_LIFE_DAYS),
)
transaction_strategy = PostgresCurrentTimePartitioningStrategy(
    size=PostgresTimePartitionSize(days=1),
    count=7,
    max_age=relativedelta(days=settings.GLITCHTIP_MAX_TRANSACTION_EVENT_LIFE_DAYS),
)

manager = PostgresPartitioningManager(
    [
        PostgresPartitioningConfig(model=IssueEvent, strategy=issue_strategy),
        PostgresPartitioningConfig(model=IssueTag, strategy=issue_strategy),
        PostgresPartitioningConfig(
            model=TransactionGroup, strategy=transaction_strategy
        ),
    ]
)
