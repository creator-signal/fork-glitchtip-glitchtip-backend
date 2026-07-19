"""
Curated, realistic data constants for make_screenshot_data command.
All data is designed to look professional in product screenshots.
"""

SCREENSHOT_ORG_NAME = "GitBot Software"
SCREENSHOT_ORG_SLUG = "gitbot-software"

SCREENSHOT_USERS = [
    {
        "email": "rob.bot@gitbot-software.io",
        "name": "Rob Bot",
        "role": 0,  # OWNER
    },
    {
        "email": "james.murphy@gitbot-software.io",
        "name": "James Murphy",
        "role": 1,  # ADMIN
    },
    {
        "email": "priya.patel@gitbot-software.io",
        "name": "Priya Patel",
        "role": 2,  # MEMBER
    },
    {
        "email": "alex.rivera@gitbot-software.io",
        "name": "Alex Rivera",
        "role": 2,  # MEMBER
    },
    {
        "email": "maria.santos@gitbot-software.io",
        "name": "Maria Santos",
        "role": 2,  # MEMBER
    },
]

SCREENSHOT_TEAMS = [
    {
        "slug": "backend",
        "member_emails": [
            "rob.bot@gitbot-software.io",
            "james.murphy@gitbot-software.io",
            "priya.patel@gitbot-software.io",
        ],
        "project_slugs": ["gitbot-api", "gitbot-worker"],
    },
    {
        "slug": "frontend",
        "member_emails": [
            "james.murphy@gitbot-software.io",
            "alex.rivera@gitbot-software.io",
            "maria.santos@gitbot-software.io",
        ],
        "project_slugs": ["gitbot-web", "gitbot-mobile"],
    },
]

SCREENSHOT_PROJECTS = [
    {"name": "gitbot-api", "platform": "python"},
    {"name": "gitbot-web", "platform": "javascript"},
    {"name": "gitbot-mobile", "platform": "react-native"},
    {"name": "gitbot-worker", "platform": "python"},
]

SCREENSHOT_ENVIRONMENTS = ["production", "staging", "development"]

SCREENSHOT_RELEASES = [
    {
        "version": "1.0.0",
        "days_ago": 45,
        "projects": ["gitbot-api", "gitbot-web"],
    },
    {
        "version": "1.1.0",
        "days_ago": 35,
        "projects": ["gitbot-api", "gitbot-web", "gitbot-mobile"],
    },
    {
        "version": "1.2.0",
        "days_ago": 25,
        "projects": ["gitbot-api", "gitbot-web"],
    },
    {
        "version": "1.2.1",
        "days_ago": 18,
        "projects": ["gitbot-api"],
    },
    {
        "version": "2.0.0",
        "days_ago": 10,
        "projects": ["gitbot-api", "gitbot-web", "gitbot-mobile", "gitbot-worker"],
    },
    {
        "version": "2.0.1",
        "days_ago": 5,
        "projects": ["gitbot-api", "gitbot-web"],
    },
    {
        "version": "2.1.0",
        "days_ago": 1,
        "projects": ["gitbot-api", "gitbot-web", "gitbot-mobile"],
    },
]

# --- SDK data per platform ---

PYTHON_SDK = {
    "name": "sentry.python",
    "version": "2.14.0",
    "packages": [{"name": "pypi:sentry-sdk", "version": "2.14.0"}],
}

JS_SDK = {
    "name": "sentry.javascript.react",
    "version": "8.34.0",
    "packages": [{"name": "npm:@sentry/react", "version": "8.34.0"}],
}

REACT_NATIVE_SDK = {
    "name": "sentry.javascript.react-native",
    "version": "5.33.0",
    "packages": [{"name": "npm:@sentry/react-native", "version": "5.33.0"}],
}

# --- Exception data per platform ---


def _python_exception(error_type, message, culprit_file, culprit_func, lineno):
    return {
        "values": [
            {
                "type": error_type,
                "value": message,
                "stacktrace": {
                    "frames": [
                        {
                            "filename": "django/core/handlers/base.py",
                            "function": "get_response",
                            "lineno": 181,
                            "in_app": False,
                        },
                        {
                            "filename": "django/core/handlers/exception.py",
                            "function": "inner",
                            "lineno": 40,
                            "in_app": False,
                        },
                        {
                            "filename": culprit_file,
                            "function": culprit_func,
                            "lineno": lineno,
                            "in_app": True,
                        },
                    ]
                },
            }
        ]
    }


def _js_exception(error_type, message, culprit_file, culprit_func, lineno, colno=12):
    return {
        "values": [
            {
                "type": error_type,
                "value": message,
                "stacktrace": {
                    "frames": [
                        {
                            "filename": "node_modules/react-dom/cjs/react-dom.development.js",
                            "function": "commitWork",
                            "lineno": 23120,
                            "colno": 7,
                            "in_app": False,
                        },
                        {
                            "filename": f"app://src/{culprit_file}",
                            "function": culprit_func,
                            "lineno": lineno,
                            "colno": colno,
                            "in_app": True,
                        },
                    ]
                },
            }
        ]
    }


# --- Issues per project ---

PYTHON_ISSUES = [
    {
        "title": "ConnectionError: Failed to establish connection to database",
        "culprit": "gitbot.db.pool.get_connection",
        "level": 4,  # ERROR
        "status": 0,  # UNRESOLVED
        "event_count": 342,
        "days_span": 14,
        "tags": {
            "environment": "production",
            "release": "2.0.1",
            "server_name": "api-prod-3.gitbot.cloud",
        },
        "exception": _python_exception(
            "ConnectionError",
            "Failed to establish connection to database",
            "gitbot/db/pool.py",
            "get_connection",
            142,
        ),
        "sdk": PYTHON_SDK,
    },
    {
        "title": "ValueError: Invalid UUID format in request body",
        "culprit": "gitbot.api.views.orders.create_order",
        "level": 4,  # ERROR
        "status": 0,  # UNRESOLVED
        "event_count": 87,
        "days_span": 7,
        "tags": {
            "environment": "production",
            "release": "2.0.0",
            "browser": "Chrome 120.0",
        },
        "exception": _python_exception(
            "ValueError",
            "Invalid UUID format in request body",
            "gitbot/api/views/orders.py",
            "create_order",
            78,
        ),
        "sdk": PYTHON_SDK,
    },
    {
        "title": "TimeoutError: Request to payment gateway timed out after 30s",
        "culprit": "gitbot.services.payments.charge",
        "level": 5,  # FATAL
        "status": 0,  # UNRESOLVED
        "event_count": 23,
        "days_span": 3,
        "tags": {
            "environment": "production",
            "release": "2.0.1",
            "server_name": "api-prod-1.gitbot.cloud",
        },
        "exception": _python_exception(
            "TimeoutError",
            "Request to payment gateway timed out after 30s",
            "gitbot/services/payments.py",
            "charge",
            215,
        ),
        "sdk": PYTHON_SDK,
    },
    {
        "title": "PermissionDenied: User does not have access to this resource",
        "culprit": "gitbot.api.middleware.auth.check_permissions",
        "level": 3,  # WARNING
        "status": 1,  # RESOLVED
        "event_count": 156,
        "days_span": 20,
        "tags": {"environment": "production", "release": "1.2.0"},
        "exception": _python_exception(
            "PermissionDenied",
            "User does not have access to this resource",
            "gitbot/api/middleware/auth.py",
            "check_permissions",
            56,
        ),
        "sdk": PYTHON_SDK,
    },
    {
        "title": "IntegrityError: duplicate key value violates unique constraint",
        "culprit": "gitbot.models.user.User.save",
        "level": 4,  # ERROR
        "status": 1,  # RESOLVED
        "event_count": 12,
        "days_span": 5,
        "tags": {"environment": "staging", "release": "2.0.0"},
        "exception": _python_exception(
            "IntegrityError",
            'duplicate key value violates unique constraint "users_email_key"',
            "gitbot/models/user.py",
            "save",
            34,
        ),
        "sdk": PYTHON_SDK,
    },
    {
        "title": "KeyError: 'shipping_address' in order processing",
        "culprit": "gitbot.services.orders.process_checkout",
        "level": 4,  # ERROR
        "status": 0,  # UNRESOLVED
        "event_count": 64,
        "days_span": 10,
        "tags": {"environment": "production", "release": "2.0.1"},
        "exception": _python_exception(
            "KeyError",
            "'shipping_address'",
            "gitbot/services/orders.py",
            "process_checkout",
            189,
        ),
        "sdk": PYTHON_SDK,
    },
    {
        "title": "SMTPAuthenticationError: Authentication failed for email service",
        "culprit": "gitbot.notifications.email.send",
        "level": 4,  # ERROR
        "status": 2,  # IGNORED
        "event_count": 8,
        "days_span": 2,
        "tags": {"environment": "staging", "release": "2.1.0"},
        "exception": _python_exception(
            "SMTPAuthenticationError",
            "Authentication failed for email service",
            "gitbot/notifications/email.py",
            "send",
            67,
        ),
        "sdk": PYTHON_SDK,
    },
    {
        "title": "ImportError: No module named 'gitbot.legacy.reports'",
        "culprit": "gitbot.tasks.generate_monthly_report",
        "level": 5,  # FATAL
        "status": 1,  # RESOLVED
        "event_count": 3,
        "days_span": 1,
        "tags": {
            "environment": "production",
            "release": "2.0.0",
            "server_name": "worker-1.gitbot.cloud",
        },
        "exception": _python_exception(
            "ImportError",
            "No module named 'gitbot.legacy.reports'",
            "gitbot/tasks.py",
            "generate_monthly_report",
            12,
        ),
        "sdk": PYTHON_SDK,
    },
    {
        "title": "DeprecationWarning: /api/v1/users endpoint sunset on 2026-04-01",
        "culprit": "gitbot.api.middleware.deprecation.check_sunset",
        "level": 2,  # INFO
        "status": 0,  # UNRESOLVED
        "event_count": 430,
        "days_span": 30,
        "tags": {"environment": "production", "release": "2.0.1"},
        "exception": _python_exception(
            "DeprecationWarning",
            "/api/v1/users endpoint sunset on 2026-04-01",
            "gitbot/api/middleware/deprecation.py",
            "check_sunset",
            28,
        ),
        "sdk": PYTHON_SDK,
    },
    {
        "title": "DebugInfo: Verbose SQL logging enabled on production",
        "culprit": "gitbot.db.middleware.QueryLogger.log",
        "level": 1,  # DEBUG
        "status": 1,  # RESOLVED
        "event_count": 1200,
        "days_span": 2,
        "tags": {
            "environment": "production",
            "release": "2.1.0",
            "server_name": "api-prod-2.gitbot.cloud",
        },
        "exception": _python_exception(
            "DebugInfo",
            "Verbose SQL logging enabled on production",
            "gitbot/db/middleware.py",
            "log",
            15,
        ),
        "sdk": PYTHON_SDK,
    },
]

JS_ISSUES = [
    {
        "title": "TypeError: Cannot read properties of undefined (reading 'map')",
        "culprit": "Dashboard.renderWidgets(dashboard.tsx)",
        "level": 4,  # ERROR
        "status": 0,  # UNRESOLVED
        "event_count": 512,
        "days_span": 21,
        "tags": {
            "environment": "production",
            "release": "2.0.1",
            "browser": "Chrome 120.0",
            "os": "Windows 11",
        },
        "exception": _js_exception(
            "TypeError",
            "Cannot read properties of undefined (reading 'map')",
            "components/Dashboard.tsx",
            "renderWidgets",
            47,
            23,
        ),
        "sdk": JS_SDK,
    },
    {
        "title": "ReferenceError: ResizeObserver is not defined",
        "culprit": "ChartComponent.mount(chart.tsx)",
        "level": 4,  # ERROR
        "status": 0,  # UNRESOLVED
        "event_count": 203,
        "days_span": 15,
        "tags": {
            "environment": "production",
            "release": "2.0.0",
            "browser": "Safari 17.2",
            "os": "macOS 14.2",
        },
        "exception": _js_exception(
            "ReferenceError",
            "ResizeObserver is not defined",
            "components/ChartComponent.tsx",
            "mount",
            31,
            8,
        ),
        "sdk": JS_SDK,
    },
    {
        "title": "ChunkLoadError: Loading chunk 'vendors' failed",
        "culprit": "webpack/runtime/ensure chunk(bootstrap.js)",
        "level": 4,  # ERROR
        "status": 0,  # UNRESOLVED
        "event_count": 47,
        "days_span": 5,
        "tags": {
            "environment": "production",
            "release": "2.0.1",
            "browser": "Firefox 121.0",
        },
        "exception": _js_exception(
            "ChunkLoadError",
            "Loading chunk 'vendors' failed",
            "webpack/runtime/bootstrap.js",
            "ensure",
            892,
            15,
        ),
        "sdk": JS_SDK,
    },
    {
        "title": "SyntaxError: Unexpected token '<' in JSON at position 0",
        "culprit": "ApiClient.parseResponse(api-client.ts)",
        "level": 4,  # ERROR
        "status": 1,  # RESOLVED
        "event_count": 178,
        "days_span": 12,
        "tags": {
            "environment": "production",
            "release": "1.2.0",
            "browser": "Chrome 119.0",
        },
        "exception": _js_exception(
            "SyntaxError",
            "Unexpected token '<' in JSON at position 0",
            "services/api-client.ts",
            "parseResponse",
            156,
            20,
        ),
        "sdk": JS_SDK,
    },
    {
        "title": "AbortError: The operation was aborted",
        "culprit": "useFetchData.cancelRequest(hooks.ts)",
        "level": 3,  # WARNING
        "status": 2,  # IGNORED
        "event_count": 891,
        "days_span": 30,
        "tags": {"environment": "production", "release": "2.0.1"},
        "exception": _js_exception(
            "AbortError",
            "The operation was aborted",
            "hooks/useFetchData.ts",
            "cancelRequest",
            42,
            18,
        ),
        "sdk": JS_SDK,
    },
    {
        "title": "RangeError: Maximum call stack size exceeded",
        "culprit": "TreeView.renderNode(tree-view.tsx)",
        "level": 5,  # FATAL
        "status": 0,  # UNRESOLVED
        "event_count": 15,
        "days_span": 3,
        "tags": {
            "environment": "production",
            "release": "2.1.0",
            "browser": "Chrome 120.0",
        },
        "exception": _js_exception(
            "RangeError",
            "Maximum call stack size exceeded",
            "components/TreeView.tsx",
            "renderNode",
            88,
            5,
        ),
        "sdk": JS_SDK,
    },
    {
        "title": "NetworkError: Failed to fetch user preferences",
        "culprit": "SettingsPage.loadPreferences(settings.tsx)",
        "level": 4,  # ERROR
        "status": 1,  # RESOLVED
        "event_count": 34,
        "days_span": 8,
        "tags": {"environment": "production", "release": "2.0.0"},
        "exception": _js_exception(
            "NetworkError",
            "Failed to fetch user preferences",
            "pages/SettingsPage.tsx",
            "loadPreferences",
            23,
            10,
        ),
        "sdk": JS_SDK,
    },
]

REACT_NATIVE_ISSUES = [
    {
        "title": "TypeError: undefined is not an object (evaluating 'user.profile.avatar')",
        "culprit": "ProfileScreen.render(ProfileScreen.js)",
        "level": 4,  # ERROR
        "status": 0,  # UNRESOLVED
        "event_count": 267,
        "days_span": 18,
        "tags": {
            "environment": "production",
            "release": "2.0.1",
            "os": "iOS 17.2",
        },
        "exception": _js_exception(
            "TypeError",
            "undefined is not an object (evaluating 'user.profile.avatar')",
            "screens/ProfileScreen.js",
            "render",
            67,
            34,
        ),
        "sdk": REACT_NATIVE_SDK,
    },
    {
        "title": "Error: Invariant Violation: requireNativeComponent: 'RCTMap' was not found",
        "culprit": "MapView.initialize(MapView.js)",
        "level": 5,  # FATAL
        "status": 0,  # UNRESOLVED
        "event_count": 42,
        "days_span": 6,
        "tags": {
            "environment": "production",
            "release": "2.0.0",
            "os": "Android 14",
        },
        "exception": _js_exception(
            "Error",
            "Invariant Violation: requireNativeComponent: 'RCTMap' was not found",
            "components/MapView.js",
            "initialize",
            15,
            8,
        ),
        "sdk": REACT_NATIVE_SDK,
    },
    {
        "title": "RangeError: Invalid array length in notifications list",
        "culprit": "NotificationList.paginate(notifications.js)",
        "level": 4,  # ERROR
        "status": 1,  # RESOLVED
        "event_count": 19,
        "days_span": 4,
        "tags": {
            "environment": "production",
            "release": "2.0.1",
            "os": "iOS 17.1",
        },
        "exception": _js_exception(
            "RangeError",
            "Invalid array length in notifications list",
            "screens/NotificationList.js",
            "paginate",
            112,
            22,
        ),
        "sdk": REACT_NATIVE_SDK,
    },
    {
        "title": "Error: Network request failed - Could not connect to server",
        "culprit": "ApiService.request(api.js)",
        "level": 3,  # WARNING
        "status": 0,  # UNRESOLVED
        "event_count": 156,
        "days_span": 25,
        "tags": {
            "environment": "production",
            "release": "2.0.0",
            "os": "Android 13",
        },
        "exception": _js_exception(
            "Error",
            "Network request failed - Could not connect to server",
            "services/api.js",
            "request",
            45,
            14,
        ),
        "sdk": REACT_NATIVE_SDK,
    },
]

WORKER_ISSUES = [
    {
        "title": "celery.exceptions.Retry: Task retry limit exceeded",
        "culprit": "gitbot.tasks.process_invoice",
        "level": 4,  # ERROR
        "status": 0,  # UNRESOLVED
        "event_count": 89,
        "days_span": 12,
        "tags": {
            "environment": "production",
            "release": "2.0.1",
            "server_name": "worker-2.gitbot.cloud",
        },
        "exception": _python_exception(
            "celery.exceptions.Retry",
            "Task retry limit exceeded",
            "gitbot/tasks.py",
            "process_invoice",
            98,
        ),
        "sdk": PYTHON_SDK,
    },
    {
        "title": "MemoryError: Unable to allocate 512 MiB for report generation",
        "culprit": "gitbot.reports.generator.build_pdf",
        "level": 5,  # FATAL
        "status": 0,  # UNRESOLVED
        "event_count": 7,
        "days_span": 3,
        "tags": {
            "environment": "production",
            "release": "2.0.0",
            "server_name": "worker-1.gitbot.cloud",
        },
        "exception": _python_exception(
            "MemoryError",
            "Unable to allocate 512 MiB for report generation",
            "gitbot/reports/generator.py",
            "build_pdf",
            234,
        ),
        "sdk": PYTHON_SDK,
    },
    {
        "title": "redis.exceptions.ConnectionError: Connection to Redis lost",
        "culprit": "gitbot.cache.redis_client.get",
        "level": 4,  # ERROR
        "status": 1,  # RESOLVED
        "event_count": 234,
        "days_span": 8,
        "tags": {
            "environment": "production",
            "release": "2.0.1",
            "server_name": "worker-3.gitbot.cloud",
        },
        "exception": _python_exception(
            "redis.exceptions.ConnectionError",
            "Connection to Redis lost",
            "gitbot/cache/redis_client.py",
            "get",
            45,
        ),
        "sdk": PYTHON_SDK,
    },
]

# Map project slug to its issues list
PROJECT_ISSUES = {
    "gitbot-api": PYTHON_ISSUES,
    "gitbot-web": JS_ISSUES,
    "gitbot-mobile": REACT_NATIVE_ISSUES,
    "gitbot-worker": WORKER_ISSUES,
}

# --- Comments and user reports for specific issues ---

SCREENSHOT_COMMENTS = [
    {
        "issue_title_prefix": "ConnectionError",
        "project": "gitbot-api",
        "user_email": "james.murphy@gitbot-software.io",
        "text": "This started happening after the 2.0.1 deploy. Looks like the connection pool isn't being recycled properly.",
    },
    {
        "issue_title_prefix": "ConnectionError",
        "project": "gitbot-api",
        "user_email": "rob.bot@gitbot-software.io",
        "text": "I've increased the pool size to 50 as a temporary fix. We need to investigate the root cause.",
    },
    {
        "issue_title_prefix": "TypeError: Cannot read",
        "project": "gitbot-web",
        "user_email": "alex.rivera@gitbot-software.io",
        "text": "This happens when the dashboard API returns an empty response. Adding a null check now.",
    },
]

SCREENSHOT_USER_REPORTS = [
    {
        "issue_title_prefix": "TypeError: Cannot read",
        "project": "gitbot-web",
        "name": "David Kim",
        "email": "david.kim@example.com",
        "comments": "The dashboard keeps showing a blank screen after I log in. Started happening today.",
    },
    {
        "issue_title_prefix": "ChunkLoadError",
        "project": "gitbot-web",
        "name": "Lisa Wang",
        "email": "lisa.w@example.com",
        "comments": "Getting a white screen when I try to load the reports page. Hard refresh doesn't fix it.",
    },
]

# --- Transactions ---

SCREENSHOT_TRANSACTIONS = [
    {
        "project": "gitbot-api",
        "transaction": "/api/v2/users",
        "op": "http.server",
        "method": "GET",
        "base_duration": 45,
        "outlier_duration": 1200,
        "count": 150,
    },
    {
        "project": "gitbot-api",
        "transaction": "/api/v2/users/{id}",
        "op": "http.server",
        "method": "GET",
        "base_duration": 30,
        "outlier_duration": 800,
        "count": 120,
    },
    {
        "project": "gitbot-api",
        "transaction": "/api/v2/orders",
        "op": "http.server",
        "method": "POST",
        "base_duration": 120,
        "outlier_duration": 3500,
        "count": 80,
    },
    {
        "project": "gitbot-api",
        "transaction": "/api/v2/orders",
        "op": "http.server",
        "method": "GET",
        "base_duration": 65,
        "outlier_duration": 2000,
        "count": 100,
    },
    {
        "project": "gitbot-api",
        "transaction": "/api/v2/products",
        "op": "http.server",
        "method": "GET",
        "base_duration": 50,
        "outlier_duration": 1500,
        "count": 90,
    },
    {
        "project": "gitbot-api",
        "transaction": "/api/v2/auth/login",
        "op": "http.server",
        "method": "POST",
        "base_duration": 80,
        "outlier_duration": 500,
        "count": 200,
    },
    {
        "project": "gitbot-api",
        "transaction": "/api/v2/search",
        "op": "http.server",
        "method": "GET",
        "base_duration": 200,
        "outlier_duration": 5000,
        "count": 60,
    },
    {
        "project": "gitbot-api",
        "transaction": "/api/v2/webhooks/stripe",
        "op": "http.server",
        "method": "POST",
        "base_duration": 150,
        "outlier_duration": 4000,
        "count": 40,
    },
    {
        "project": "gitbot-web",
        "transaction": "/dashboard",
        "op": "pageload",
        "method": "",
        "base_duration": 800,
        "outlier_duration": 4000,
        "count": 100,
    },
    {
        "project": "gitbot-web",
        "transaction": "/orders",
        "op": "pageload",
        "method": "",
        "base_duration": 600,
        "outlier_duration": 3000,
        "count": 80,
    },
    {
        "project": "gitbot-web",
        "transaction": "/settings",
        "op": "pageload",
        "method": "",
        "base_duration": 400,
        "outlier_duration": 2000,
        "count": 50,
    },
    {
        "project": "gitbot-web",
        "transaction": "/login",
        "op": "pageload",
        "method": "",
        "base_duration": 300,
        "outlier_duration": 1500,
        "count": 120,
    },
]

# --- Logs ---

SCREENSHOT_LOG_SERVICES = {
    "gitbot-api": {
        "services": ["gitbot-api"],
        "hosts": [
            "api-prod-1.gitbot.cloud",
            "api-prod-2.gitbot.cloud",
            "api-prod-3.gitbot.cloud",
        ],
    },
    "gitbot-web": {
        "services": ["gitbot-web-ssr"],
        "hosts": ["web-prod-1.gitbot.cloud", "web-prod-2.gitbot.cloud"],
    },
    "gitbot-worker": {
        "services": ["gitbot-worker", "gitbot-scheduler"],
        "hosts": ["worker-1.gitbot.cloud", "worker-2.gitbot.cloud"],
    },
}

SCREENSHOT_LOG_MESSAGES = {
    "info": [
        "Request completed: GET /api/v2/users - 200 in {ms}ms",
        "Request completed: POST /api/v2/orders - 201 in {ms}ms",
        "User authenticated successfully: user_id={user_id}",
        "Order ord_{order_id} processed successfully",
        "Cache hit for key: session:{user_id}",
        "Health check passed - all services operational",
        "Background job completed: send_invoice in {ms}ms",
        "Email notification sent to user {user_id}",
    ],
    "warn": [
        "Slow query detected: {query_time}ms on orders table",
        "Rate limit approaching for API key ak_3f8b...",
        "Memory usage at {percent}% on {host}",
        "Retry attempt {attempt}/3 for task process_invoice",
        "SSL certificate expires in {days} days for api.gitbot.dev",
        "Queue depth reaching threshold: {count}/1000 items",
    ],
    "error": [
        "Failed to connect to database: connection refused on port 5432",
        "Payment processing failed for order ord_{order_id}: gateway timeout",
        "Unhandled exception in OrdersView: ValueError",
        "S3 upload failed: bucket access denied for invoice_{order_id}.pdf",
        "Authentication token expired for session sess_{session_id}",
    ],
    "fatal": [
        "Database connection pool exhausted - no connections available",
        "Out of memory: worker process killed by OOM killer on {host}",
    ],
    "debug": [
        "Parsing request body: content-type=application/json",
        "Cache miss for key: product:{product_id}, fetching from database",
        "SQL: SELECT * FROM orders WHERE user_id = {user_id} LIMIT 50",
    ],
}

# --- Uptime Monitors ---

SCREENSHOT_MONITORS = [
    {
        "name": "API Health Check",
        "url": "https://api.gitbot.dev/health",
        "interval": 60,
        "monitor_type": "Ping",
        "expected_status": 200,
        "project": "gitbot-api",
        "is_healthy": True,
    },
    {
        "name": "Web Application",
        "url": "https://app.gitbot.dev",
        "interval": 180,
        "monitor_type": "GET",
        "expected_status": 200,
        "project": "gitbot-web",
        "is_healthy": True,
    },
    {
        "name": "CDN Assets",
        "url": "https://cdn.gitbot.dev/assets/health.txt",
        "interval": 300,
        "monitor_type": "GET",
        "expected_status": 200,
        "project": "gitbot-web",
        "is_healthy": True,
    },
    {
        "name": "Payment Gateway",
        "url": "https://payments.gitbot.dev/status",
        "interval": 60,
        "monitor_type": "GET",
        "expected_status": 200,
        "project": "gitbot-api",
        "is_healthy": False,  # Recent downtime incident
    },
    {
        "name": "Mobile API",
        "url": "https://mobile-api.gitbot.dev/ping",
        "interval": 120,
        "monitor_type": "Ping",
        "expected_status": 200,
        "project": "gitbot-mobile",
        "is_healthy": True,
    },
    {
        "name": "Database Connection Pool",
        "url": "https://api.gitbot.dev/internal/db-health",
        "interval": 30,
        "monitor_type": "GET",
        "expected_status": 200,
        "project": "gitbot-api",
        "is_healthy": True,
    },
    {
        "name": "Auth Service",
        "url": "https://auth.gitbot.dev/status",
        "interval": 60,
        "monitor_type": "GET",
        "expected_status": 200,
        "project": "gitbot-api",
        "is_healthy": True,
    },
    {
        "name": "Webhook Delivery",
        "url": "https://webhooks.gitbot.dev/health",
        "interval": 300,
        "monitor_type": "Ping",
        "expected_status": 200,
        "project": "gitbot-worker",
        "is_healthy": True,
    },
    {
        "name": "Search Index",
        "url": "https://api.gitbot.dev/internal/search-health",
        "interval": 600,
        "monitor_type": "GET",
        "expected_status": 200,
        "project": "gitbot-api",
        "is_healthy": True,
    },
    {
        "name": "SSL Certificate",
        "url": "https://app.gitbot.dev",
        "interval": 21600,
        "monitor_type": "SSL",
        "expected_status": None,
        "project": "gitbot-web",
        "is_healthy": True,
    },
    {
        "name": "Background Jobs Queue",
        "url": "https://api.gitbot.dev/internal/queue-health",
        "interval": 120,
        "monitor_type": "GET",
        "expected_status": 200,
        "project": "gitbot-worker",
        "is_healthy": True,
    },
    {
        "name": "Staging Environment",
        "url": "https://staging.gitbot.dev",
        "interval": 600,
        "monitor_type": "Ping",
        "expected_status": 200,
        "project": "gitbot-web",
        "is_healthy": False,  # Staging is flaky
    },
]

# --- Alerts ---

SCREENSHOT_ALERTS = [
    {
        "name": "High Error Rate - API",
        "project": "gitbot-api",
        "timespan_minutes": 5,
        "quantity": 10,
        "uptime": False,
        "recipients": [
            {"type": "email"},
            {
                "type": "webhook",
                "url": "https://hooks.slack.com/services/T00000/B00000/XXXXXXXX",
            },
        ],
    },
    {
        "name": "Critical Errors - Web",
        "project": "gitbot-web",
        "timespan_minutes": 15,
        "quantity": 25,
        "uptime": False,
        "recipients": [
            {"type": "email"},
        ],
    },
    {
        "name": "Uptime Alert - API",
        "project": "gitbot-api",
        "timespan_minutes": None,
        "quantity": None,
        "uptime": True,
        "recipients": [
            {"type": "email"},
            {
                "type": "webhook",
                "url": "https://discord.com/api/webhooks/000000/xxxx",
            },
        ],
    },
]
