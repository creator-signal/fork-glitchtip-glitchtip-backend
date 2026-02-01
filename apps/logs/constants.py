from glitchtip.model_utils import FromStringIntegerChoices


class LogLevel(FromStringIntegerChoices):
    """
    Log severity levels following OpenTelemetry semantic conventions.
    Maps to severity_number ranges in OTel spec.
    """

    TRACE = 0, "trace"
    DEBUG = 1, "debug"
    INFO = 2, "info"
    WARN = 3, "warn"
    ERROR = 4, "error"
    FATAL = 5, "fatal"
