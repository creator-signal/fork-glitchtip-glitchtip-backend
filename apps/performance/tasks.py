import logging

from django_vtasks import task

logger = logging.getLogger(__name__)


@task
def promote_spans():
    from apps.performance.promotion import promote_spans as _promote

    _promote()


@task
def compact_span_chunks():
    from apps.performance.promotion import compact_span_chunks as _compact

    _compact()
