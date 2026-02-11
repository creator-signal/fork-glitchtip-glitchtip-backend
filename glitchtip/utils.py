import random
import string

from django.conf import settings


def get_random_string(length=16):
    letters = string.ascii_lowercase
    result_str = "".join(random.choice(letters) for i in range(length))
    return result_str


def get_read_db() -> str:
    """Get the database alias for read operations (replica if available)."""
    return "read_only" if "read_only" in settings.DATABASES else "default"
