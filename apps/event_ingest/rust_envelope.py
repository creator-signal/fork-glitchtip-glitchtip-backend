"""Rust-backed request-body decompression + envelope framing for event ingest.

``gt_rust`` is a hard dependency of the ingest path. There is no Python
decompression fallback and no enable/disable flag: **all** ``Content-Encoding``
handling (gzip / deflate / br / zstd) for every ingest endpoint runs in Rust's
allocator via the ``gt_rust`` extension. This module is what replaced the old
``glitchtip.middleware.DecompressBodyMiddleware`` and its hand-rolled streaming
decoders.

Why move this off Python:

  - **Decompression.** The middleware streamed the compressed body through a
    Python decoder, materializing the decompressed payload on the Python heap
    before the view saw it. ``gt_rust`` decompresses in Rust with the GIL
    released and a hard size cap, so a zip bomb is stopped before it balloons
    pymalloc arenas — the win is *bounded memory per request*, not raw speed.
  - **Envelope framing.** ``request.body`` + ``BytesIO``/``readline`` framing
    allocates per line, a steady drip into the arenas that fragment under high
    async concurrency. ``gt_rust`` frames in Rust and hands back only the item
    payload bytes. Item *schema validation* stays in Python (Pydantic) — the
    deliberate seam; it can move into Rust later behind this same boundary.

Two entry points, by endpoint shape:

  - ``frame_envelope`` — the ``/envelope/`` hot path: decompress **and** frame
    in one pass (``parse_envelope``).
  - ``decompress_body`` — the non-envelope endpoints (``/store/``,
    ``/security/``, ``/minidump/``): decompress only, hand the raw bytes to the
    endpoint's own parser.

Both take the raw (still-compressed) request body plus the original
``Content-Encoding``; with the middleware gone, nothing decompresses upstream.
"""

from urllib.parse import urlparse

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from gt_rust.envelope import (
    EnvelopeTooBig,
    ParsedEnvelope,
    decompress,
    parse_envelope,
    parse_envelope_header,
)

__all__ = [
    "EnvelopeTooBig",
    "request_content_encoding",
    "frame_envelope",
    "decompress_body",
    "sentry_key_from_envelope_body",
    "envelope_error_response",
]

# Content-Encoding values gt_rust can decode. Anything else (absent,
# ``identity``, or an unknown token) means "no decompression" — the body is
# handed through as-is, matching how the old middleware only wrapped the stream
# for these four encodings.
_DECODABLE = frozenset({"gzip", "deflate", "br", "zstd"})


def request_content_encoding(request: HttpRequest) -> str | None:
    """The request's decodable ``Content-Encoding``, or ``None``.

    Returns one of ``_DECODABLE`` (lower-cased) when present, else ``None`` so
    callers can skip decompression entirely for plain bodies.
    """
    encoding = (request.META.get("HTTP_CONTENT_ENCODING") or "").lower()
    return encoding if encoding in _DECODABLE else None


def _max_unzipped() -> int:
    """The decompressed-size cap, formerly enforced by the middleware."""
    return settings.GLITCHTIP_MAX_UNZIPPED_PAYLOAD_SIZE


def frame_envelope(body: bytes, content_encoding: str | None = None) -> ParsedEnvelope:
    """Decompress (if needed) and frame an envelope body via gt_rust.

    Raises ``ValueError`` for malformed framing and ``EnvelopeTooBig`` for an
    oversized decompressed payload — translate with ``envelope_error_response``.
    """
    return parse_envelope(body, content_encoding, _max_unzipped())


def decompress_body(body: bytes, content_encoding: str | None) -> bytes:
    """Decompress a full request body via gt_rust — no envelope framing.

    For the non-envelope endpoints that parse the raw bytes themselves. Raises
    ``ValueError`` for a garbled/unsupported stream and ``EnvelopeTooBig`` when
    the decompressed size exceeds ``GLITCHTIP_MAX_UNZIPPED_PAYLOAD_SIZE``.
    """
    return decompress(body, content_encoding, _max_unzipped())


def sentry_key_from_envelope_body(
    body: bytes, content_encoding: str | None = None
) -> str | None:
    """Lift the DSN public key from the envelope header, or ``None``.

    Reads only the first framed line (no full-body framing). Returns the public
    key string (``urlparse(dsn).username``, matching ``embed_auth``), suitable
    for the same ``UUID(...)`` parse the query/header auth paths use. Never
    raises — a malformed/oversized body just means "no key here", and the normal
    Invalid-DSN rejection takes over.
    """
    try:
        header = parse_envelope_header(body, content_encoding, _max_unzipped())
    except (ValueError, EnvelopeTooBig):
        return None
    if not header.dsn:
        return None
    return urlparse(header.dsn).username or None


def envelope_error_response(exc: Exception) -> HttpResponse | None:
    """Map a gt_rust framing/decompression error to the view's HTTP response."""
    if isinstance(exc, EnvelopeTooBig):
        return HttpResponse(str(exc), status=413)
    if isinstance(exc, ValueError):
        return JsonResponse({"detail": "Invalid envelope header"}, status=400)
    return None
