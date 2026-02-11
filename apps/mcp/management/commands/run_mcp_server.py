from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Run the MCP server over stdio for local development"

    def handle(self, *args, **options):
        from apps.mcp.server import mcp

        mcp.run(transport="stdio")
