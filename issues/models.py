from django.db import models

from glitchtip.model_utils import FromStringIntegerChoices


class EventType(models.IntegerChoices):
    DEFAULT = 0, "default"
    ERROR = 1, "error"
    CSP = 2, "csp"
    TRANSACTION = 3, "transaction"


class EventStatus(FromStringIntegerChoices):
    UNRESOLVED = 0, "unresolved"
    RESOLVED = 1, "resolved"
    IGNORED = 2, "ignored"
