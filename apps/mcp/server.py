import json
import logging

from django.core.exceptions import FieldError
from django.http import Http404
from mcp.server.fastmcp import FastMCP

from . import data, serializers
from .auth import validate_token

logger = logging.getLogger(__name__)

mcp = FastMCP("glitchtip", stateless_http=True)


def _error(message: str) -> str:
    return json.dumps({"error": message})


async def _auth(token: str, required_scopes: list[str]) -> int:
    """Validate token, check scopes, and return user_id.

    Raises ValueError if the token is invalid or lacks required scopes.
    """
    user_id, scopes = await validate_token(token)
    if not any(s in required_scopes for s in scopes):
        raise ValueError("Token lacks required scope")
    return user_id


@mcp.tool()
async def list_organizations(token: str) -> str:
    """List all organizations the authenticated user has access to."""
    try:
        user_id = await _auth(token, ["org:read", "org:write", "org:admin"])
        orgs = await data.get_organizations(user_id)
        return json.dumps([serializers.serialize_organization(o) for o in orgs])
    except ValueError as e:
        return _error(str(e))


@mcp.tool()
async def list_projects(token: str, organization_slug: str) -> str:
    """List all projects in an organization."""
    try:
        user_id = await _auth(token, ["project:read", "project:write", "project:admin"])
        projects = await data.get_projects(user_id, organization_slug)
        return json.dumps([serializers.serialize_project(p) for p in projects])
    except ValueError as e:
        return _error(str(e))


@mcp.tool()
async def list_issues(
    token: str,
    organization_slug: str,
    project_slug: str | None = None,
    query: str | None = None,
    sort: str | None = None,
    limit: int = 25,
) -> str:
    """List issues for an organization, optionally filtered by project.

    By default returns all issues (including resolved). Use query="is:unresolved"
    to show only active issues, which is usually what you want for investigating
    current problems.

    Args:
        token: API authentication token
        organization_slug: Organization slug
        project_slug: Optional project slug to filter by
        query: Search query. Examples: "is:unresolved", "is:resolved",
            "level:error", or free text search. Combine with spaces.
        sort: Sort field: "-last_seen" (default), "-count", "-priority",
            "-first_seen"
        limit: Max issues to return (default 25, max 100)
    """
    try:
        user_id = await _auth(token, ["event:read", "event:write", "event:admin"])
        if limit < 1:
            return _error("limit must be at least 1")
        issues = await data.get_issues(
            user_id,
            organization_slug,
            project_slug=project_slug,
            query=query,
            sort=sort,
            limit=limit,
        )
        return json.dumps([serializers.serialize_issue(i) for i in issues])
    except ValueError as e:
        return _error(str(e))
    except Http404:
        return _error(f"Organization '{organization_slug}' not found")
    except FieldError:
        return _error(f"Invalid sort field: {sort}")


@mcp.tool()
async def get_issue(token: str, issue_id: int) -> str:
    """Get details for a single issue by ID."""
    try:
        user_id = await _auth(token, ["event:read", "event:write", "event:admin"])
        issue = await data.get_issue(user_id, issue_id)
        if issue is None:
            return _error("Issue not found")
        return json.dumps(serializers.serialize_issue(issue))
    except ValueError as e:
        return _error(str(e))


@mcp.tool()
async def get_latest_event(token: str, issue_id: int) -> str:
    """Get the latest event for an issue."""
    try:
        user_id = await _auth(token, ["event:read", "event:write", "event:admin"])
        event = await data.get_latest_event(user_id, issue_id)
        if event is None:
            return _error("No events found for this issue")
        return json.dumps(serializers.serialize_event(event))
    except ValueError as e:
        return _error(str(e))


@mcp.tool()
async def get_event(token: str, event_id: str) -> str:
    """Look up a specific event by its ID and return it with its parent issue.

    Accepts either format:
    - GlitchTip UUIDv7 (e.g. "019c4a0ac93a75f0a26ef22012159759")
    - Sentry SDK event_id UUID (e.g. "a1b2c3d4-e5f6-7890-abcd-ef1234567890")

    Use this when a user provides an event ID from a URL, log, or alert.

    Args:
        token: API authentication token
        event_id: Event UUID (either GlitchTip id or Sentry SDK event_id)
    """
    try:
        user_id = await _auth(token, ["event:read", "event:write", "event:admin"])
        event = await data.get_event(user_id, event_id)
        if event is None:
            return _error("Event not found")
        result = serializers.serialize_event(event)
        result["issue"] = serializers.serialize_issue(event.issue)
        return json.dumps(result)
    except ValueError as e:
        return _error(str(e))


@mcp.tool()
async def list_alerts(
    token: str,
    organization_slug: str,
    project_slug: str | None = None,
) -> str:
    """List alert rules for an organization, optionally filtered by project.

    Args:
        token: API authentication token
        organization_slug: Organization slug
        project_slug: Optional project slug to filter by
    """
    try:
        user_id = await _auth(token, ["project:read", "project:write", "project:admin"])
        alerts = await data.get_alerts(
            user_id, organization_slug, project_slug=project_slug
        )
        return json.dumps([serializers.serialize_alert(a) for a in alerts])
    except ValueError as e:
        return _error(str(e))


@mcp.tool()
async def list_monitors(token: str, organization_slug: str) -> str:
    """List uptime monitors for an organization."""
    try:
        user_id = await _auth(token, ["project:read", "project:write", "project:admin"])
        monitors = await data.get_monitors(user_id, organization_slug)
        return json.dumps([serializers.serialize_monitor(m) for m in monitors])
    except ValueError as e:
        return _error(str(e))
