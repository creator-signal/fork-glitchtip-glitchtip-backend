import logging

from django_vtasks import task

from apps.performance.promotion import BATCH_LIMIT

logger = logging.getLogger(__name__)


@task
def promote_spans():
    from apps.performance.promotion import promote_spans as _promote

    promoted = _promote()
    if promoted >= BATCH_LIMIT:
        # More rows likely remain — schedule another run immediately
        logger.info("Promotion batch full (%d rows), re-enqueueing", promoted)
        promote_spans.enqueue()


@task
def compact_span_chunks():
    from apps.performance.promotion import compact_span_chunks as _compact

    _compact()
