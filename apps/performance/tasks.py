import logging

from django_vtasks import task

logger = logging.getLogger(__name__)


@task
def promote_spans():
    from apps.performance.promotion import promote_spans as _promote

    promoted, truncated = _promote()
    if truncated:
        # At least one org hit the per-org batch limit — more rows likely remain
        logger.info("Promotion batch full (%d rows), re-enqueueing", promoted)
        promote_spans.enqueue()


@task
def compact_span_chunks():
    from apps.performance.promotion import compact_span_chunks as _compact

    _compact()
