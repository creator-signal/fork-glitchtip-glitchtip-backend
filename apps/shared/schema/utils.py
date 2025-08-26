import logging
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError, ValidationInfo, ValidatorFunctionWrapHandler

from .error import EventProcessingError

logger = logging.getLogger(__name__)


def invalid_to_none(v: Any, handler: Callable[[Any], Any]) -> Any:
    try:
        return handler(v)
    except ValidationError:
        return None


class ValidationErrorMarker:
    """A simple marker to indicate that validation failed for a field."""

    def __init__(self, error: EventProcessingError):
        self.error = error


def report_error_on_fail(
    v: Any, handler: ValidatorFunctionWrapHandler, info: ValidationInfo
) -> Any | ValidationErrorMarker:
    """
    Pydantic WrapValidator that attempts to validate a field.

    On failure, it logs a warning and returns a ValidationErrorMarker
    containing the structured error details.
    """
    try:
        # Attempt the standard validation.
        return handler(v)
    except ValidationError as e:
        first_error = e.errors()[0]
        error_type = first_error.get("type", "validation_error")
        error_msg = first_error.get("msg", "Unknown validation error")
        logger.warning(
            f"Field '{info.field_name}': Validation failed. "
            f"Value: '{str(v)[:50]}' ({type(v).__name__}), Error: {error_msg}. "
            "Marking for nullification."
        )

        # Return a marker with the structured error payload.
        processing_error = EventProcessingError(
            type=error_type,
            name=info.field_name,
            value=v,
        )
        return ValidationErrorMarker(error=processing_error)
