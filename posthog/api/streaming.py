import time
<<<<<<< HEAD
import asyncio
=======
import random
>>>>>>> 853edf79a59 (feat(infra): per-process SSE concurrency cap with 503 and jittered Retry-After)
from collections.abc import AsyncIterable, AsyncIterator, Iterable, Iterator
from http import HTTPStatus

from django.conf import settings
from django.db import connections
from django.http import HttpResponse, StreamingHttpResponse

from prometheus_client import Counter, Gauge, Histogram

# What StreamingHttpResponse actually accepts: sync or async iterables of bytes or
# str chunks (Django encodes str via the response charset). Broad on purpose — SSE
# views yield str, proxies yield bytes, and the empty-stream stub passes a list.
StreamContent = Iterable[bytes | str] | AsyncIterable[bytes | str]

# Disable proxy buffering/caching so SSE chunks reach the client immediately
# (nginx/Envoy in front of web-django otherwise buffer the stream).
_SSE_DEFAULT_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
}

# The `endpoint` label is a static name passed by each SSE view — never a raw
# request path, which would blow up label cardinality.
SSE_OPEN_CONNECTIONS_GAUGE = Gauge(
    "posthog_open_sse_connections",
    "SSE streams currently being served by this process",
    labelnames=["endpoint"],
    multiprocess_mode="livesum",
)
SSE_STREAM_OPENED_COUNTER = Counter(
    "posthog_sse_stream_opened_total",
    "SSE streams that started being consumed",
    labelnames=["endpoint"],
)
SSE_STREAM_CLOSED_COUNTER = Counter(
    "posthog_sse_stream_closed_total",
    "SSE streams that ended, by outcome",
    labelnames=["endpoint", "outcome"],
)
# Streams legitimately run for many minutes (rotation caps them at ~15 min),
# so buckets extend well past the default 10s ceiling.
SSE_STREAM_DURATION_HISTOGRAM = Histogram(
    "posthog_sse_stream_duration_seconds",
    "Wall-clock lifetime of an SSE stream, from first chunk pulled to close",
    labelnames=["endpoint"],
    buckets=(1, 5, 15, 60, 180, 420, 900, 1200, float("inf")),
)


SSE_REJECTED_OVER_CAP_COUNTER = Counter(
    "posthog_sse_rejected_over_cap_total",
    "SSE streams rejected with 503 because the per-process concurrency cap was reached",
    labelnames=["endpoint"],
)

# Per-process count of streams currently being consumed, kept in step with the
# open-connections gauge (incremented at first pull, decremented on close).
# Plain int mutation is safe here: both servers we run mutate it from a single
# event loop per process, and the GIL covers the WSGI fallback.
_active_stream_count = 0

# Rejected clients get "come back in base + [0, jitter) seconds" so a burst that
# hits the cap spreads its retries out instead of reconnecting in lockstep.
_RETRY_AFTER_BASE_SECONDS = 15
_RETRY_AFTER_JITTER_SECONDS = 30


def _record_stream_open(endpoint: str) -> None:
    global _active_stream_count
    _active_stream_count += 1
    SSE_STREAM_OPENED_COUNTER.labels(endpoint=endpoint).inc()
    SSE_OPEN_CONNECTIONS_GAUGE.labels(endpoint=endpoint).inc()


def _record_stream_close(endpoint: str, outcome: str, started_at: float) -> None:
    global _active_stream_count
    _active_stream_count -= 1
    SSE_OPEN_CONNECTIONS_GAUGE.labels(endpoint=endpoint).dec()
    SSE_STREAM_CLOSED_COUNTER.labels(endpoint=endpoint, outcome=outcome).inc()
    SSE_STREAM_DURATION_HISTOGRAM.labels(endpoint=endpoint).observe(time.monotonic() - started_at)


def _over_stream_cap() -> bool:
    cap = settings.SSE_MAX_CONCURRENT_STREAMS_PER_PROCESS
    return cap is not None and _active_stream_count >= cap


def _stream_cap_rejection(endpoint: str) -> HttpResponse:
    SSE_REJECTED_OVER_CAP_COUNTER.labels(endpoint=endpoint).inc()
    retry_after = _RETRY_AFTER_BASE_SECONDS + random.randrange(_RETRY_AFTER_JITTER_SECONDS)
    return HttpResponse(
        status=HTTPStatus.SERVICE_UNAVAILABLE,
        headers={"Retry-After": str(retry_after), **_SSE_DEFAULT_HEADERS},
    )


async def _instrumented_aiter(stream: AsyncIterable[bytes | str], endpoint: str) -> AsyncIterator[bytes | str]:
    """Pass chunks through untouched, tracking open count, outcome, and duration.

    Metric work happens only at stream start and end — nothing is added per
    chunk. A client disconnect surfaces here as cancellation of the generator
    (``GeneratorExit`` from ``aclose()``, or ``asyncio.CancelledError`` when the
    ASGI handler cancels the streaming task), which is why both get their own
    outcome rather than folding into ``error``.
    """
    _record_stream_open(endpoint)
    started_at = time.monotonic()
    outcome = "completed"
    try:
        async for chunk in stream:
            yield chunk
    except (GeneratorExit, asyncio.CancelledError):
        outcome = "client_disconnect"
        raise
    except BaseException:
        outcome = "error"
        raise
    finally:
        _record_stream_close(endpoint, outcome, started_at)


def _instrumented_iter(stream: Iterable[bytes | str], endpoint: str) -> Iterator[bytes | str]:
    _record_stream_open(endpoint)
    started_at = time.monotonic()
    outcome = "completed"
    try:
        yield from stream
    except GeneratorExit:
        outcome = "client_disconnect"
        raise
    except BaseException:
        outcome = "error"
        raise
    finally:
        _record_stream_close(endpoint, outcome, started_at)


def _instrument_stream(stream: StreamContent, endpoint: str) -> StreamContent:
    if isinstance(stream, AsyncIterable):
        return _instrumented_aiter(stream, endpoint)
    return _instrumented_iter(stream, endpoint)


def _release_request_connections() -> None:
    """Close this thread's DB connections, unless a transaction is open.

    Closes unconditionally (``conn.close()``) rather than via
    ``close_if_unusable_or_obsolete()``, which only closes connections past their
    ``CONN_MAX_AGE`` — that would make this helper a silent no-op if the setting
    ever became nonzero, re-pinning a pgbouncer slot per stream. Closing an idle
    autocommit connection is always safe; Django reopens on next use.

    Connections inside an atomic block are skipped: severing an open transaction
    corrupts it. PostHog never streams from inside ``transaction.atomic()``, so in
    production this closes everything; the case that does hit the guard is Django
    ``TestCase``'s per-test transaction wrapper, which the test client only shields
    from the signal-dispatched ``close_old_connections``, not from direct calls
    like this one.
    """
    for conn in connections.all(initialized_only=True):
        if not conn.in_atomic_block:
            conn.close()


def streaming_response(
    stream: StreamContent,
    *,
    content_type: str,
    status: int = HTTPStatus.OK,
    headers: dict[str, str] | None = None,
) -> StreamingHttpResponse:
    """Build a ``StreamingHttpResponse``, releasing request-thread DB connections first.

    Use this (or ``sse_streaming_response`` for SSE) instead of constructing
    ``StreamingHttpResponse`` directly — a semgrep rule enforces it. See
    ``sse_streaming_response`` for why releasing connections matters.

    The stream body must not rely on the request-thread connection: do any
    in-stream DB work through ``posthog.sync.database_sync_to_async`` so it
    acquires and releases its own connection.
    """
    _release_request_connections()
    return StreamingHttpResponse(
        stream,
        status=status,
        content_type=content_type,
        headers=headers or {},
    )


def sse_streaming_response(
    stream: StreamContent,
    *,
    endpoint: str = "unknown",
    status: int = HTTPStatus.OK,
    headers: dict[str, str] | None = None,
) -> StreamingHttpResponse | HttpResponse:
    """Build a ``text/event-stream`` response for a long-lived SSE endpoint.

    Use this instead of constructing ``StreamingHttpResponse`` directly. It
    enforces the invariant that's otherwise easy to forget:

        sync DB work before a long-lived SSE stream must release its connection
        before streaming starts.

    PostHog runs with ``CONN_MAX_AGE = 0``, so any connection still open when the
    stream begins (from authentication, team resolution, ``get_object``, or
    serializer/ORM reads in the sync view) stays pinned to a pgbouncer client
    slot for the *entire* stream — ``request_finished`` only frees it once the
    stream ends, which for SSE is many minutes. At scale that turns every
    concurrent subscriber into a held connection and exhausts the pool. Releasing
    the request-thread connections here frees them before the stream starts.

    The stream body must not rely on the request-thread connection: do any
    in-stream DB work through ``posthog.sync.database_sync_to_async`` so it
    acquires and releases its own connection.

    Limitation: this runs at view-return time, but response-phase middleware runs
    after the view returns and before the stream body is consumed — middleware
    that touches the DB in ``process_response`` lazily reopens a connection that
    then stays pinned for the whole stream. Keep response middleware DB-free on
    SSE paths.

    ``endpoint`` is a static, low-cardinality name for the stream (e.g.
    ``"wizard_session"``) used as the label on the SSE connection metrics.

    Admission control: when this process is already serving
    ``SSE_MAX_CONCURRENT_STREAMS_PER_PROCESS`` streams, the stream is not opened
    and the client gets ``503`` with a jittered ``Retry-After``. ``EventSource``
    treats that as transient and reconnects later, so overload degrades into
    delayed reconnects instead of pinned processes and starved health probes.
    The check is advisory (concurrent admissions can briefly overshoot the cap);
    its job is stopping unbounded pile-up, not enforcing an exact ceiling.
    """
    if _over_stream_cap():
        return _stream_cap_rejection(endpoint)
    return streaming_response(
        _instrument_stream(stream, endpoint),
        content_type="text/event-stream",
        status=status,
        headers={**_SSE_DEFAULT_HEADERS, **(headers or {})},
    )
