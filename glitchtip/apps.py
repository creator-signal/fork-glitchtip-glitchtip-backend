from django.apps import AppConfig


class GlitchtipConfig(AppConfig):
    name = "glitchtip"

    def ready(self):
        from django_async_backend.db import async_connections
        from django_vtasks.signals import async_task_failure, async_task_finished

        async def close_async_db_connections(**kwargs):
            await async_connections.close_all()

        async_task_finished.connect(close_async_db_connections)
        async_task_failure.connect(close_async_db_connections)
