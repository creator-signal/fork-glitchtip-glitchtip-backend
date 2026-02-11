from base64 import b64decode, b64encode
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib import parse
from uuid import UUID

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import connections
from django.http import Http404, HttpResponse
from django.shortcuts import aget_object_or_404
from ninja import Query, Router

from apps.organizations_ext.models import Organization
from apps.projects.models import LogProjectHourlyStatistic
from glitchtip.api.authentication import AuthHttpRequest
from glitchtip.api.pagination import set_pagination_headers
from glitchtip.api.permissions import has_permission
from glitchtip.partition_manager import UUID7Helper
from glitchtip.utils import get_read_db

from .constants import LogLevel
from .models import LogService, compute_service_hash
from .schema import (
    LogEventSchema,
    LogFilterSchema,
    LogServiceSchema,
    LogStatsFilterSchema,
    LogStatsSchema,
)

router = Router()

# Map string levels to LogLevel enum values
LEVEL_MAP = {
    "trace": LogLevel.TRACE,
    "debug": LogLevel.DEBUG,
    "info": LogLevel.INFO,
    "warn": LogLevel.WARN,
    "warning": LogLevel.WARN,
    "error": LogLevel.ERROR,
    "fatal": LogLevel.FATAL,
}

# Default time range for queries (prevents unbounded queries)
DEFAULT_LOOKBACK_DAYS = 7

# Days of hot storage before data is archived to cold
HOT_STORAGE_DAYS = getattr(settings, "GLITCHTIP_LOGS_HOT_DAYS", 7)


def decode_cursor(cursor_str: str | None) -> UUID | None:
    """Decode cursor string to get position (log UUID)."""
    if not cursor_str:
        return None
    try:
        decoded = b64decode(cursor_str).decode()
        tokens = parse.parse_qs(decoded, keep_blank_values=True)
        position = tokens.get("p", [None])[0]
        if position:
            return UUID(position)
    except (ValueError, TypeError):
        pass
    return None


def encode_cursor(position: UUID) -> str:
    """Encode log UUID as cursor string."""
    querystring = f"p={position}"
    return b64encode(querystring.encode()).decode()


def get_organization_for_user(user_id: int, organization_slug: str):
    """Get organization queryset filtered by user membership."""
    return Organization.objects.filter(users=user_id, slug=organization_slug)


@dataclass
class LogEventRow:
    """Row from query, compatible with LogEventSchema resolvers."""

    id: UUID
    trace_id: UUID | None
    organization_id: int
    project_id: int
    span_id: int | None
    level: int
    severity_number: int | None
    body: str
    service: str
    data: dict

    @property
    def timestamp(self) -> datetime:
        """Derive timestamp from UUIDv7 id."""
        return UUID7Helper.extract_datetime(self.id)


def _parse_data_field(data) -> dict:
    """Parse data field which may be dict, string, or None."""
    import json

    if data is None:
        return {}
    if isinstance(data, dict):
        return data
    if isinstance(data, str):
        try:
            return json.loads(data)
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _row_to_log_event(row: tuple) -> LogEventRow:
    """Convert a database row (positional) to LogEventRow."""
    return LogEventRow(
        id=row[0] if isinstance(row[0], UUID) else UUID(str(row[0])),
        trace_id=(
            row[1]
            if isinstance(row[1], UUID)
            else (UUID(str(row[1])) if row[1] else None)
        ),
        organization_id=row[2],
        project_id=row[3],
        span_id=row[4],
        level=row[5],
        severity_number=row[6],
        body=row[7],
        service=row[8],
        data=_parse_data_field(row[9]),
    )


def query_hot_storage(
    organization_id: int,
    start_dt: datetime,
    end_dt: datetime,
    project_ids: list[int] | None = None,
    level_values: list[int] | None = None,
    service: str | None = None,
    trace_id: str | None = None,
    query: str | None = None,
    limit: int = 100,
    cursor_position: UUID | None = None,
) -> list[LogEventRow]:
    """
    Query logs from hot storage (PostgreSQL partitioned table).

    Uses UUIDv7 range for efficient partition pruning.
    """
    where_clauses = ["organization_id = %s"]
    params: list = [organization_id]

    # Time range via UUIDv7 bounds for partition pruning
    start_uuid, end_uuid = UUID7Helper.get_range_for_date(start_dt, end_dt)
    where_clauses.append("id >= %s")
    where_clauses.append("id < %s")
    params.extend([str(start_uuid), str(end_uuid)])

    if project_ids:
        placeholders = ",".join(["%s"] * len(project_ids))
        where_clauses.append(f"project_id IN ({placeholders})")
        params.extend(project_ids)

    if level_values:
        placeholders = ",".join(["%s"] * len(level_values))
        where_clauses.append(f"level IN ({placeholders})")
        params.extend(level_values)

    if service:
        where_clauses.append("service ILIKE %s")
        params.append(f"%{service}%")

    if trace_id:
        where_clauses.append("trace_id = %s")
        params.append(trace_id)

    if query:
        where_clauses.append("body ILIKE %s")
        params.append(f"%{query}%")

    # Cursor-based pagination: fetch items with id < cursor
    if cursor_position:
        where_clauses.append("id < %s")
        params.append(str(cursor_position))

    where_sql = " AND ".join(where_clauses)

    sql = f"""
        SELECT id, trace_id, organization_id, project_id, span_id,
               level, severity_number, body, service, data
        FROM logs_logevent
        WHERE {where_sql}
        ORDER BY id DESC
        LIMIT %s
    """
    params.append(limit)

    db_alias = get_read_db()
    results = []

    with connections[db_alias].cursor() as cursor:
        cursor.execute(sql, params)
        for row in cursor.fetchall():
            results.append(_row_to_log_event(row))

    return results


def query_cold_storage(
    organization_id: int,
    start_dt: datetime,
    end_dt: datetime,
    project_ids: list[int] | None = None,
    level_values: list[int] | None = None,
    service: str | None = None,
    trace_id: str | None = None,
    query: str | None = None,
    limit: int = 100,
    cursor_position: UUID | None = None,
) -> list[LogEventRow]:
    """
    Query logs from cold storage (per-org Parquet files via DuckDB).

    Uses glob pattern to read all files for the org, then filters by
    UUIDv7 timestamp. DuckDB's predicate pushdown enables row group
    skipping based on the UUID filter for efficiency.
    """
    from .cold_storage import (
        COLD_STORAGE_PREFIX,
        ColdStorageConfig,
        is_pg_duckdb_available,
        setup_duckdb_s3_credentials,
    )

    if not is_pg_duckdb_available():
        return []

    config = ColdStorageConfig.from_settings()
    if not config.bucket:
        return []

    # Use glob pattern to read all files for this org
    # pg_duckdb doesn't support array syntax, but globs work
    glob_path = f"s3://{config.bucket}/{COLD_STORAGE_PREFIX}/logs_logevent/org_{organization_id}/*.parquet"

    # Build WHERE clause using r['column'] syntax required by pg_duckdb
    where_parts = [f"r['organization_id'] = {organization_id}"]

    # Time range via UUIDv7 bounds - this enables efficient filtering
    start_uuid, end_uuid = UUID7Helper.get_range_for_date(start_dt, end_dt)
    where_parts.append(f"r['id'] >= '{start_uuid}'::UUID")
    where_parts.append(f"r['id'] < '{end_uuid}'::UUID")

    if project_ids:
        ids_str = ",".join(str(p) for p in project_ids)
        where_parts.append(f"r['project_id'] IN ({ids_str})")

    if level_values:
        lvls_str = ",".join(str(lv) for lv in level_values)
        where_parts.append(f"r['level'] IN ({lvls_str})")

    if service:
        # Escape single quotes
        svc = service.replace("'", "''")
        where_parts.append(f"r['service'] ILIKE '%{svc}%'")

    if trace_id:
        where_parts.append(f"r['trace_id'] = '{trace_id}'::UUID")

    if query:
        # Escape single quotes
        q = query.replace("'", "''")
        where_parts.append(f"r['body'] ILIKE '%{q}%'")

    # Cursor-based pagination
    if cursor_position:
        where_parts.append(f"r['id'] < '{cursor_position}'::UUID")

    where_sql = " AND ".join(where_parts)

    try:
        setup_duckdb_s3_credentials(config)

        with connections["default"].cursor() as cursor:
            cursor.execute("SET duckdb.force_execution = true;")

            # pg_duckdb requires r['column'] syntax for read_parquet
            sql = f"""
                SELECT r['id']::uuid AS id,
                       r['trace_id']::uuid AS trace_id,
                       r['organization_id']::bigint AS organization_id,
                       r['project_id']::bigint AS project_id,
                       r['span_id']::bigint AS span_id,
                       r['level']::smallint AS level,
                       r['severity_number']::smallint AS severity_number,
                       r['body']::text AS body,
                       r['service']::varchar AS service,
                       r['data']::json AS data
                FROM read_parquet('{glob_path}') r
                WHERE {where_sql}
                ORDER BY r['id'] DESC
                LIMIT {limit};
            """
            cursor.execute(sql)

            results = []
            for row in cursor.fetchall():
                results.append(_row_to_log_event(row))
            return results

    except Exception as e:
        error_str = str(e)
        # Handle missing files gracefully
        if "No files found" in error_str or "Could not open" in error_str:
            return []
        # Handle other DuckDB errors gracefully for cold storage
        if "duckdb" in error_str.lower():
            return []
        raise


def query_logs_combined(
    organization_id: int,
    start_dt: datetime,
    end_dt: datetime,
    project_ids: list[int] | None = None,
    level_values: list[int] | None = None,
    service: str | None = None,
    trace_id: str | None = None,
    query: str | None = None,
    limit: int = 100,
    cursor_position: UUID | None = None,
) -> list[LogEventRow]:
    """
    Query logs from both hot and cold storage.

    Hot storage (PostgreSQL) is queried first for recent data.
    Cold storage (Parquet via DuckDB) is queried for older data if needed.

    Results are merged and sorted by id DESC.
    """
    now = datetime.now(timezone.utc)
    hot_cutoff = now - timedelta(days=HOT_STORAGE_DAYS)

    results = []

    # Query hot storage if date range overlaps
    if end_dt > hot_cutoff:
        hot_start = max(start_dt, hot_cutoff)
        hot_results = query_hot_storage(
            organization_id=organization_id,
            start_dt=hot_start,
            end_dt=end_dt,
            project_ids=project_ids,
            level_values=level_values,
            service=service,
            trace_id=trace_id,
            query=query,
            limit=limit,
            cursor_position=cursor_position,
        )
        results.extend(hot_results)

    # Query cold storage if date range extends before hot cutoff
    # and we haven't filled the limit yet
    if start_dt < hot_cutoff and len(results) < limit:
        cold_end = min(end_dt, hot_cutoff)
        cold_limit = limit - len(results)
        # For cold storage, use cursor if hot returned nothing, else use last hot result
        cold_cursor = cursor_position if not results else results[-1].id
        cold_results = query_cold_storage(
            organization_id=organization_id,
            start_dt=start_dt,
            end_dt=cold_end,
            project_ids=project_ids,
            level_values=level_values,
            service=service,
            trace_id=trace_id,
            query=query,
            limit=cold_limit,
            cursor_position=cold_cursor,
        )
        results.extend(cold_results)

    # Sort combined results by id DESC (most recent first)
    results.sort(key=lambda r: r.id, reverse=True)

    return results[:limit]


def get_log_by_id(organization_id: int, log_id: UUID) -> LogEventRow | None:
    """
    Get a single log by ID, checking both hot and cold storage.

    Uses UUIDv7 timestamp to determine which storage tier to query first.
    """
    # Extract timestamp from UUIDv7 to know where to look
    log_time = UUID7Helper.extract_datetime(log_id)
    now = datetime.now(timezone.utc)
    hot_cutoff = now - timedelta(days=HOT_STORAGE_DAYS)

    # Try hot storage first if log is recent
    if log_time > hot_cutoff:
        sql = """
            SELECT id, trace_id, organization_id, project_id, span_id,
                   level, severity_number, body, service, data
            FROM logs_logevent
            WHERE id = %s AND organization_id = %s
            LIMIT 1
        """
        db_alias = get_read_db()
        with connections[db_alias].cursor() as cursor:
            cursor.execute(sql, [str(log_id), organization_id])
            row = cursor.fetchone()
            if row:
                return _row_to_log_event(row)

    # Try cold storage
    from .cold_storage import (
        ColdStorageConfig,
        get_org_cold_s3_path,
        is_pg_duckdb_available,
        setup_duckdb_s3_credentials,
    )

    if not is_pg_duckdb_available():
        return None

    config = ColdStorageConfig.from_settings()
    if not config.bucket:
        return None

    date_str = log_time.strftime("%Y%m%d")
    s3_path = get_org_cold_s3_path(config, "logs_logevent", organization_id, date_str)

    try:
        setup_duckdb_s3_credentials(config)

        with connections["default"].cursor() as cursor:
            cursor.execute("SET duckdb.force_execution = true;")

            sql = f"""
                SELECT id, trace_id, organization_id, project_id, span_id,
                       level, severity_number, body, service, data
                FROM read_parquet('{s3_path}')
                WHERE id = '{log_id}'::UUID AND organization_id = {organization_id}
                LIMIT 1;
            """
            cursor.execute(sql)
            row = cursor.fetchone()
            if row:
                return _row_to_log_event(row)

    except Exception as e:
        error_str = str(e)
        if "No files found" in error_str or "Could not open" in error_str:
            pass  # File doesn't exist, log not in cold storage
        else:
            raise

    return None


@router.get(
    "organizations/{slug:organization_slug}/logs/",
    response=list[LogEventSchema],
    by_alias=True,
)
@has_permission(["event:read", "event:write", "event:admin"])
async def list_logs(
    request: AuthHttpRequest,
    response: HttpResponse,
    organization_slug: str,
    filters: Query[LogFilterSchema],
):
    """
    List log events for an organization with optional filtering.

    Queries hot storage (PostgreSQL) for recent data and cold storage
    (S3 Parquet via DuckDB) for older data seamlessly.

    Supports filtering by:
    - project: List of project IDs
    - level: List of log levels (trace, debug, info, warn, error, fatal)
    - service: Service name (partial match)
    - traceId: Trace ID for correlation
    - query: Full-text search in log body
    - start/end: Time range filtering (defaults to last 7 days)
    - cursor: Pagination cursor for "load more"
    - limit: Results per page (1-200, default 100)
    """
    organization = await aget_object_or_404(
        get_organization_for_user(request.auth.user_id, organization_slug)
    )

    # Determine time range (default to last 7 days)
    now = datetime.now(timezone.utc)
    start_dt = filters.start or (now - timedelta(days=DEFAULT_LOOKBACK_DAYS))
    end_dt = filters.end or now

    # Parse level filters
    level_values = None
    if filters.level:
        level_values = []
        for level_str in filters.level:
            level_enum = LEVEL_MAP.get(level_str.lower())
            if level_enum is not None:
                level_values.append(level_enum)

    # Decode cursor
    cursor_position = decode_cursor(filters.cursor)
    limit = filters.limit

    # Fetch limit + 1 to detect if there's a next page
    results = await sync_to_async(query_logs_combined)(
        organization_id=organization.id,
        start_dt=start_dt,
        end_dt=end_dt,
        project_ids=filters.project,
        level_values=level_values,
        service=filters.service,
        trace_id=filters.trace_id,
        query=filters.query,
        limit=limit + 1,
        cursor_position=cursor_position,
    )

    # Check if there's a next page
    has_next = len(results) > limit
    page_results = results[:limit]

    # Build pagination headers
    next_cursor = None
    if has_next and page_results:
        next_cursor = encode_cursor(page_results[-1].id)

    set_pagination_headers(
        response, request, has_next, next_cursor, hits=len(page_results)
    )

    return page_results


@router.get(
    "organizations/{slug:organization_slug}/logs/{uuid:log_id}/",
    response=LogEventSchema,
    by_alias=True,
)
@has_permission(["event:read", "event:write", "event:admin"])
async def get_log(
    request: AuthHttpRequest,
    organization_slug: str,
    log_id: UUID,
):
    """Get a single log event by ID (searches both hot and cold storage)."""
    organization = await aget_object_or_404(
        get_organization_for_user(request.auth.user_id, organization_slug)
    )

    log_event = await sync_to_async(get_log_by_id)(organization.id, log_id)

    if not log_event:
        raise Http404("Log event not found")

    return log_event


def query_log_stats(
    organization_id: int,
    start_dt: datetime,
    end_dt: datetime,
    project_ids: list[int] | None = None,
    level_values: list[int] | None = None,
    service_buckets: list[int] | None = None,
) -> dict:
    """
    Query log statistics from PostgreSQL.

    Returns data grouped by level with hourly counts.
    """
    from django.db.models import Sum
    from django.db.models.functions import TruncHour

    qs = LogProjectHourlyStatistic.objects.filter(
        organization_id=organization_id,
        date__gte=start_dt,
        date__lt=end_dt,
    )

    if project_ids:
        qs = qs.filter(project_id__in=project_ids)

    if level_values:
        qs = qs.filter(level__in=level_values)

    if service_buckets:
        qs = qs.filter(service_bucket__in=service_buckets)

    # Group by hour and level, sum counts
    qs = (
        qs.annotate(hour=TruncHour("date"))
        .values("hour", "level")
        .annotate(total=Sum("count"))
        .order_by("hour", "level")
    )

    # Build result structure
    # Collect all hours and levels
    hours_set: set[datetime] = set()
    level_data: dict[int, dict[datetime, int]] = {}

    for row in qs:
        hour = row["hour"]
        level = row["level"]
        total = row["total"]

        hours_set.add(hour)
        if level not in level_data:
            level_data[level] = {}
        level_data[level][hour] = total

    # Sort hours
    intervals = sorted(hours_set)

    # Build series for each level
    series = []
    for level_int in sorted(level_data.keys()):
        level_name = LogLevel(level_int).label
        data = [level_data[level_int].get(hour, 0) for hour in intervals]
        series.append({"name": level_name, "data": data})

    return {"intervals": intervals, "series": series}


@router.get(
    "organizations/{slug:organization_slug}/logs/stats/",
    response=LogStatsSchema,
    by_alias=True,
)
@has_permission(["event:read", "event:write", "event:admin"])
async def get_log_stats(
    request: AuthHttpRequest,
    response: HttpResponse,
    organization_slug: str,
    filters: Query[LogStatsFilterSchema],
):
    """
    Get log statistics for an organization.

    Returns hourly counts grouped by level for charting.
    Supports filtering by project and level.
    Time range defaults to last 7 days, max 90 days.
    """
    organization = await aget_object_or_404(
        get_organization_for_user(request.auth.user_id, organization_slug)
    )

    # Determine time range (default to last 7 days, max 90 days)
    now = datetime.now(timezone.utc)
    start_dt = filters.start or (now - timedelta(days=DEFAULT_LOOKBACK_DAYS))
    end_dt = filters.end or now

    # Enforce max 90 day range
    max_range = timedelta(days=90)
    if end_dt - start_dt > max_range:
        start_dt = end_dt - max_range

    # Parse level filters
    level_values = None
    if filters.level:
        level_values = []
        for level_str in filters.level:
            level_enum = LEVEL_MAP.get(level_str.lower())
            if level_enum is not None:
                level_values.append(level_enum)

    # Parse service filters to hash buckets
    service_buckets = None
    if filters.service:
        service_buckets = [compute_service_hash(s) for s in filters.service]

    # Query stats
    result = await sync_to_async(query_log_stats)(
        organization_id=organization.id,
        start_dt=start_dt,
        end_dt=end_dt,
        project_ids=filters.project,
        level_values=level_values,
        service_buckets=service_buckets,
    )

    return result


@router.get(
    "organizations/{slug:organization_slug}/logs/services/",
    response=list[LogServiceSchema],
    by_alias=True,
)
@has_permission(["event:read", "event:write", "event:admin"])
async def list_log_services(
    request: AuthHttpRequest,
    organization_slug: str,
):
    """
    List unique service names for an organization.

    Returns services ordered by last_seen (most recent first).
    Used to populate filter dropdowns in the UI.
    """
    organization = await aget_object_or_404(
        get_organization_for_user(request.auth.user_id, organization_slug)
    )

    services = [
        s
        async for s in LogService.objects.filter(organization=organization)
        .order_by("-last_seen")
        .values("name", "last_seen")[:100]  # Limit to 100 most recent
    ]

    return services
