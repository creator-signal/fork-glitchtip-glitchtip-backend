import shlex
from typing import Literal
from uuid import UUID

from django.contrib.postgres.search import SearchQuery
from django.db.models import ExpressionWrapper, F, FloatField, Q, QuerySet, Value
from django.db.models.functions import Extract, Log
from ninja import Query

from .models import EventStatus, LogLevel
from .schema import IssueFilters


sort_options = Literal[
    "last_seen",
    "first_seen",
    "count",
    "priority",
    "-last_seen",
    "-first_seen",
    "-count",
    "-priority",
]


def _get_text_search_filter(search_query_str: str) -> Q:
    """
    Builds the Q object for free-text search against the IssueSearchIndex model.
    This is the single source of truth for text search logic.
    """
    terms = shlex.split(search_query_str)
    final_filter = Q()

    for term in terms:
        # Use the related_name `search_index_record` to traverse from Issue
        # to the IssueSearchIndex table.

        # 1. Build the pattern matching part (for pattern_text)
        if "*" in term:
            # Use the custom 'ilike' lookup which is more performant.
            # We wrap in '%' to find the pattern anywhere in the string.
            pattern = f"%{term.replace('*', '%')}%"
            pattern_q = Q(search_index__pattern_text__ilike=pattern)
        else:
            pattern_q = Q(search_index__pattern_text__icontains=term)

        # 2. Build the FTS part (for fts_document)
        # We strip '*' as FTS only supports prefix matching, which is handled
        # by our append_and_limit_tsvector function, not user input.
        fts_q = Q(search_index__fts_document=SearchQuery(term.replace("*", "")))

        # 3. Combine them: a match in either field is valid
        term_filter = pattern_q | fts_q
        final_filter &= term_filter

    return final_filter


def filter_issue_list(
    qs: QuerySet,
    filters: Query[IssueFilters],
    sort: sort_options | None = None,
    event_id: UUID | None = None,
) -> QuerySet:
    """
    Applies a full set of filters to an Issue queryset.
    """
    qs_filters = filters.dict(exclude_none=True)
    query = qs_filters.pop("query", None)

    if environment_filters := qs_filters.pop("environment", None):
        qs = qs.filter(
            issuetag__tag_key__key="environment",
            issuetag__tag_value__value__in=environment_filters,
        )
    if qs_filters:
        qs = qs.filter(**qs_filters)

    if event_id:
        return qs.filter(issueevent__id=event_id)

    if query:
        structured_queries = {}
        text_search_parts = []
        queries = shlex.split(query)

        # Separate structured queries from free-text search
        for part in queries:
            if ":" in part:
                query_name, query_value = part.split(":", 1)
                structured_queries[query_name] = query_value.strip('"')
            else:
                text_search_parts.append(part)

        # Apply structured filters
        if status := structured_queries.get("is"):
            qs = qs.filter(status=EventStatus.from_string(status))
        if has_tag := structured_queries.get("has"):
            qs = qs.filter(issuetag__tag_key__key=has_tag)
        if level := structured_queries.get("level"):
            qs = qs.filter(level=LogLevel.from_string(level))

        # Apply tag filters
        for key, value in structured_queries.items():
            if key not in ["is", "has", "level"]:
                qs = qs.filter(
                    issuetag__tag_key__key=key,
                    issuetag__tag_value__value=value,
                )

        # Apply free-text search filter
        if text_search_parts:
            text_search_query = " ".join(text_search_parts)
            qs = qs.filter(_get_text_search_filter(text_search_query))
    if sort:
        if sort.endswith("priority"):
            qs = qs.annotate(
                priority=ExpressionWrapper(
                    Log(10, F("count"))
                    + Extract(F("last_seen"), "epoch") / Value(300000.0),
                    output_field=FloatField(),
                )
            )
        qs = qs.order_by(sort)

    return qs
