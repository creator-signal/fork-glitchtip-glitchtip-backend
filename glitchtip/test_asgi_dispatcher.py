from django.test import SimpleTestCase
from glitchtip.asgi import MCPDjangoDispatcher

class MockASGIApp:
    def __init__(self):
        self.called = False
        self.scope = None

    async def __call__(self, scope, receive, send):
        self.called = True
        self.scope = scope

class MCPDjangoDispatcherTestCase(SimpleTestCase):
    async def test_mcp_routing_strips_prefix(self):
        mock_django = MockASGIApp()
        mock_mcp = MockASGIApp()
        dispatcher = MCPDjangoDispatcher(mock_django, mock_mcp, mcp_prefix="/mcp")

        scope = {
            "type": "http",
            "path": "/mcp/sse",
            "method": "GET",
        }

        async def mock_receive(): pass
        async def mock_send(message): pass

        await dispatcher(scope, mock_receive, mock_send)

        self.assertTrue(mock_mcp.called)
        self.assertFalse(mock_django.called)
        
        # Verify path stripping
        self.assertEqual(mock_mcp.scope["path"], "/sse")
        # Verify root_path setting
        self.assertEqual(mock_mcp.scope["root_path"], "/mcp")

    async def test_mcp_routing_root_path(self):
        mock_django = MockASGIApp()
        mock_mcp = MockASGIApp()
        dispatcher = MCPDjangoDispatcher(mock_django, mock_mcp, mcp_prefix="/mcp")

        scope = {
            "type": "http",
            "path": "/mcp",
            "method": "GET",
        }

        async def mock_receive(): pass
        async def mock_send(message): pass

        await dispatcher(scope, mock_receive, mock_send)

        self.assertTrue(mock_mcp.called)
        self.assertEqual(mock_mcp.scope["path"], "/")
        self.assertEqual(mock_mcp.scope["root_path"], "/mcp")

    async def test_django_routing(self):
        mock_django = MockASGIApp()
        mock_mcp = MockASGIApp()
        dispatcher = MCPDjangoDispatcher(mock_django, mock_mcp, mcp_prefix="/mcp")

        scope = {
            "type": "http",
            "path": "/api/0/projects/",
            "method": "GET",
        }

        async def mock_receive(): pass
        async def mock_send(message): pass

        await dispatcher(scope, mock_receive, mock_send)

        self.assertTrue(mock_django.called)
        self.assertFalse(mock_mcp.called)
        self.assertEqual(mock_django.scope["path"], "/api/0/projects/")
