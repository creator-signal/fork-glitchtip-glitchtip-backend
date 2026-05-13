from django.apps import AppConfig


class GlitchtipConfig(AppConfig):
    name = "glitchtip"

    def ready(self):
        from . import task_signals  # noqa: F401
