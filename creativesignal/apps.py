from django.apps import AppConfig


class CreatorSignalConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "creativesignal"

    def ready(self):
        from creativesignal import signals  # noqa: F401
