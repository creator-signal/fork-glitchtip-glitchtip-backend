import logging
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError
from django.utils import timezone
from django_vtasks import task

from apps.event_ingest.schema import (
    InterchangeTransactionEvent,
    IssueTaskMessage,
    UserReportTaskMessage,
)
from apps.issue_events.models import IssueEvent, UserReport
from glitchtip.partition_manager import UUID7Helper

from .process_event import process_issue_events, process_transaction_events

logger = logging.getLogger(__name__)


@task(queue_name="ingest")
def ingest_event(tasks: list):
    logger.info(f"Process {len(tasks)} issue event requests")
    read_only_db = "read_only" if "read_only" in settings.DATABASES else "default"
    process_issue_events(
        [IssueTaskMessage(**task["args"][0]) for task in tasks],
        read_only_db=read_only_db,
    )


@task(queue_name="ingest")
def ingest_transaction(tasks: list):
    logger.info(f"Process {len(tasks)} transaction event requests")
    read_only_db = "read_only" if "read_only" in settings.DATABASES else "default"
    process_transaction_events(
        [InterchangeTransactionEvent(**task["args"][0]) for task in tasks],
        read_only_db=read_only_db,
    )


@task(queue_name="ingest")
async def ingest_user_report(tasks: list):
    for t in tasks:
        msg = UserReportTaskMessage(**t["args"][0])
        issue_id = None
        if msg.event_id:
            event_uuid = uuid.UUID(msg.event_id)
            # Constrain to recent UUID7 range + org for partition pruning
            recent_lower = UUID7Helper.from_datetime(
                timezone.now() - timedelta(hours=1)
            )
            issue_event = await (
                IssueEvent.objects.filter(
                    event_id=event_uuid,
                    id__gte=recent_lower,
                    organization_id=msg.organization_id,
                )
                .only("issue_id")
                .afirst()
            )
            if issue_event:
                issue_id = issue_event.issue_id
        else:
            event_uuid = uuid.uuid4()

        try:
            await UserReport.objects.acreate(
                project_id=msg.project_id,
                issue_id=issue_id,
                event_id=event_uuid,
                name=msg.name,
                email=msg.email,
                comments=msg.comments,
            )
        except IntegrityError:
            pass  # Duplicate report
