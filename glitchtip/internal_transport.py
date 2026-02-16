import contextvars
import logging
from dataclasses import asdict

from sentry_sdk.transport import Transport

logger = logging.getLogger(__name__)

# Equivalent to old Sentry's NOOP_HUB — suppresses SDK capture during
# internal event processing to prevent synchronous recursion.
_processing_internal = contextvars.ContextVar("_processing_internal", default=False)


class InternalTransport(Transport):
    """
    Custom SDK transport that bypasses HTTP and injects events directly
    into the ingest task queue. Prevents self-DOS feedback loops when
    SENTRY_DSN points to the same GlitchTip instance.

    Inspired by old open-source Sentry's InternalTransport
    (src/sentry/utils/sdk.py).
    """

    def __init__(self, options=None):
        super().__init__(options)
        self._project_key = None

    @property
    def project_key(self):
        """Lazy lookup — avoids import/DB access at init time."""
        if self._project_key is None:
            from apps.projects.models import ProjectKey

            if self.parsed_dsn:
                try:
                    self._project_key = ProjectKey.objects.select_related(
                        "project"
                    ).get(public_key=self.parsed_dsn.public_key)
                except ProjectKey.DoesNotExist:
                    logger.warning("InternalTransport: project key not found for DSN")
        return self._project_key

    def capture_envelope(self, envelope):
        token = _processing_internal.set(True)
        try:
            self._process_envelope(envelope)
        except Exception:
            logger.exception("InternalTransport: failed to process envelope")
        finally:
            _processing_internal.reset(token)

    def _process_envelope(self, envelope):
        from django.utils import timezone

        from apps.event_ingest.interfaces import IngestTaskMessage
        from apps.event_ingest.tasks import ingest_event
        from apps.event_ingest.utils import serialize_for_vtasks
        from apps.issue_events.constants import IssueEventType
        from glitchtip.partition_manager import UUID7Helper

        key = self.project_key
        if key is None:
            return

        event = envelope.get_event()
        if event is None:
            return  # Only handle error events for now

        # Determine issue type from event data
        issue_type = (
            IssueEventType.ERROR if "exception" in event else IssueEventType.DEFAULT
        )

        msg = IngestTaskMessage(
            project_id=key.project_id,
            organization_id=key.project.organization_id,
            payload=event | {"type": issue_type},
            received=timezone.now(),
            update_first_event=False,
            uuid=UUID7Helper.from_datetime().hex,
        )
        ingest_event.enqueue(serialize_for_vtasks(asdict(msg)))
