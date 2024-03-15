from glitchtip.model_utils import FromStringIntegerChoices


class LogLevel(FromStringIntegerChoices):
    NOTSET = 0, "sample"
    DEBUG = 1, "debug"
    INFO = 2, "info"
    WARNING = 3, "warning"
    ERROR = 4, "error"
    FATAL = 5, "fatal"

    @classmethod
    def from_string(cls, string: str):
        result = super().from_string(string)
        if result:
            return result
        if string == "critical":
            return cls.FATAL
        if string == "log":
            return cls.INFO
        return cls.ERROR
