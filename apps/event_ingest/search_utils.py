import os

from .interfaces import ProcessingEvent
from .schema import ErrorIssueEventSchema
from .utils import remove_bad_chars

SEARCH_MAX_FTS_LEN = 3000
SEARCH_MAX_PATTERN_LEN = 3000
MAX_FRAMES_PER_STACKTRACE = 3


# --- Helper function for capping logic ---
def _apply_capping_logic(parts: set[str], max_len: int) -> str:
    """Sorts, joins, and truncates a set of string parts."""
    full_string = " ".join(sorted(list(parts)))
    if len(full_string) > max_len:
        limit_idx = full_string.rfind(" ", 0, max_len)
        if limit_idx != -1:
            full_string = full_string[:limit_idx]
        else:
            full_string = full_string[:max_len]
    return remove_bad_chars(full_string)


# --- New Specialized Builders ---
def build_fts_string(event: ProcessingEvent) -> str:
    """Builds the string for the `fts_document` (linguistic search)."""
    fts_parts: set[str] = set()
    payload = event.payload

    if title := event.title:
        fts_parts.add(title)

    # The exception VALUE is linguistic prose and belongs in FTS.
    if isinstance(payload, ErrorIssueEventSchema) and (
        exc := payload.exception_most_recent()
    ):
        if exc.value:
            fts_parts.add(exc.value)

    return _apply_capping_logic(fts_parts, SEARCH_MAX_FTS_LEN)


def build_pattern_text_string(event: ProcessingEvent) -> str:
    """Builds the string for `pattern_text` (literal/wildcard search)."""
    pattern_parts: set[str] = set()
    payload = event.payload

    # The title is both literal and linguistic.
    if title := event.title:
        pattern_parts.add(title)

    if transaction := event.transaction:
        pattern_parts.add(transaction)

    # The exception TYPE is a literal token.
    if isinstance(payload, ErrorIssueEventSchema) and (
        exc := payload.exception_most_recent()
    ):
        if exc.type:
            pattern_parts.add(exc.type)
        frames_from_this_stacktrace = 0
        if stacktrace := exc.stacktrace:
            for frame in stacktrace.frames:
                if frames_from_this_stacktrace >= MAX_FRAMES_PER_STACKTRACE:
                    break
                if filename := frame.filename:
                    pattern_parts.add(os.path.basename(str(filename)))
                    frames_from_this_stacktrace += 1

    if request := getattr(payload, "request", None):
        if url := getattr(request, "url", None):
            pattern_parts.add(url)

    return _apply_capping_logic(pattern_parts, SEARCH_MAX_PATTERN_LEN)
