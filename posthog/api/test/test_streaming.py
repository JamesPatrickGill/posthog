import asyncio
from collections.abc import AsyncIterator, Iterator
from http import HTTPStatus
from typing import cast

from unittest import mock

from django.http import StreamingHttpResponse
from django.http.response import HttpResponseBase
from django.test import override_settings

from prometheus_client import REGISTRY

from posthog.api.streaming import _instrument_stream, sse_streaming_response, streaming_response


def _gen() -> Iterator[bytes]:
    yield b"data: hello\n\n"


class TestSSEStreamingResponse:
    def test_releases_db_connections_before_streaming(self):
        idle = mock.Mock(in_atomic_block=False)
        with mock.patch("posthog.api.streaming.connections") as connections:
            connections.all.return_value = [idle]
            sse_streaming_response(_gen())
        connections.all.assert_called_once_with(initialized_only=True)
        idle.close.assert_called_once()

    def test_does_not_sever_connections_with_an_open_transaction(self):
        in_transaction = mock.Mock(in_atomic_block=True)
        with mock.patch("posthog.api.streaming.connections") as connections:
            connections.all.return_value = [in_transaction]
            sse_streaming_response(_gen())
        in_transaction.close.assert_not_called()

    def test_sets_event_stream_content_type_and_default_headers(self):
        response = sse_streaming_response(_gen())
        assert isinstance(response, StreamingHttpResponse)
        assert response.headers["Content-Type"] == "text/event-stream"
        assert response.headers["Cache-Control"] == "no-cache, no-transform"
        assert response.headers["X-Accel-Buffering"] == "no"
        assert response.status_code == HTTPStatus.OK

    def test_caller_headers_and_status_override_defaults(self):
        response = sse_streaming_response(
            _gen(),
            status=HTTPStatus.ACCEPTED,
            headers={"Cache-Control": "no-cache", "X-Custom": "1"},
        )
        assert response.status_code == HTTPStatus.ACCEPTED
        assert response.headers["Cache-Control"] == "no-cache"
        assert response.headers["X-Accel-Buffering"] == "no"
        assert response.headers["X-Custom"] == "1"


def _sync_content(response: HttpResponseBase) -> Iterator[bytes]:
    # sse_streaming_response returns a union (it can 503); in these non-capped
    # tests the response is always a stream, so narrow and cast.
    assert isinstance(response, StreamingHttpResponse)
    return cast(Iterator[bytes], response.streaming_content)


def _async_content(response: HttpResponseBase) -> AsyncIterator[bytes]:
    assert isinstance(response, StreamingHttpResponse)
    return cast(AsyncIterator[bytes], response.streaming_content)


def _open_connections(endpoint: str) -> float:
    return REGISTRY.get_sample_value("posthog_open_sse_connections", {"endpoint": endpoint}) or 0.0


def _closed_total(endpoint: str, outcome: str) -> float:
    return (
        REGISTRY.get_sample_value("posthog_sse_stream_closed_total", {"endpoint": endpoint, "outcome": outcome}) or 0.0
    )


class TestSSEStreamMetrics:
    # If the gauge ever fails to decrement on an exit path, it reads permanently
    # inflated — and anything keyed on it (dashboards, connection-based
    # autoscaling) sees phantom load. These tests pin every exit path.

    def test_sync_stream_counts_open_and_completed(self):
        response = sse_streaming_response(_gen(), endpoint="test_sync_complete")
        assert b"".join(_sync_content(response)) == b"data: hello\n\n"
        assert _open_connections("test_sync_complete") == 0.0
        assert _closed_total("test_sync_complete", "completed") == 1.0

    def test_sync_stream_error_decrements_gauge_and_counts_error(self):
        def boom() -> Iterator[bytes]:
            yield b"data: one\n\n"
            raise RuntimeError("stream died")

        response = sse_streaming_response(boom(), endpoint="test_sync_error")
        it = _sync_content(response)
        next(it)
        try:
            next(it)
        except RuntimeError:
            pass
        assert _open_connections("test_sync_error") == 0.0
        assert _closed_total("test_sync_error", "error") == 1.0

    def test_sync_stream_early_close_counts_client_disconnect(self):
        def endless() -> Iterator[bytes]:
            while True:
                yield b": ping\n\n"

        response = sse_streaming_response(endless(), endpoint="test_sync_disconnect")
        it = _sync_content(response)
        next(it)
        assert _open_connections("test_sync_disconnect") == 1.0
        response.close()  # what Django does when the client goes away
        assert _open_connections("test_sync_disconnect") == 0.0
        assert _closed_total("test_sync_disconnect", "client_disconnect") == 1.0

    async def test_async_stream_counts_open_and_completed(self):
        async def agen():
            yield b"data: hello\n\n"

        response = sse_streaming_response(agen(), endpoint="test_async_complete")
        assert [chunk async for chunk in _async_content(response)] == [b"data: hello\n\n"]
        assert _open_connections("test_async_complete") == 0.0
        assert _closed_total("test_async_complete", "completed") == 1.0

    async def test_async_stream_early_close_counts_client_disconnect(self):
        async def endless():
            while True:
                yield b": ping\n\n"

        # Django registers the instrumented iterator itself as the response's
        # resource closer, so closing it directly is the disconnect path — the
        # outer streaming_content wrapper does not propagate aclose() eagerly.
        stream = _instrument_stream(endless(), "test_async_disconnect")
        assert isinstance(stream, AsyncIterator)
        await stream.__anext__()
        assert _open_connections("test_async_disconnect") == 1.0
        await stream.aclose()  # type: ignore[attr-defined]
        assert _open_connections("test_async_disconnect") == 0.0
        assert _closed_total("test_async_disconnect", "client_disconnect") == 1.0


class TestStreamingResponse:
    def test_honors_content_type_and_does_not_inject_sse_headers(self):
        # Non-SSE callers (audio, proxies) rely on the general wrapper passing their
        # content_type through and NOT forcing the SSE-only proxy-buffering header.
        response = streaming_response(_gen(), content_type="audio/mpeg")
        assert response.headers["Content-Type"] == "audio/mpeg"
        assert "X-Accel-Buffering" not in response.headers
        assert response.status_code == HTTPStatus.OK


class TestSSEAsyncCancellation:
    async def test_task_cancellation_counts_client_disconnect_not_error(self):
        first_chunk_pulled = asyncio.Event()

        async def blocking():
            yield b": ping\n\n"
            await asyncio.Event().wait()  # park forever; cancellation lands here

        stream = _instrument_stream(blocking(), "test_async_cancel")
        assert isinstance(stream, AsyncIterator)

        async def consume():
            async for _ in stream:
                first_chunk_pulled.set()

        task = asyncio.ensure_future(consume())
        await first_chunk_pulled.wait()
        assert _open_connections("test_async_cancel") == 1.0
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert _open_connections("test_async_cancel") == 0.0
        assert _closed_total("test_async_cancel", "client_disconnect") == 1.0
        assert _closed_total("test_async_cancel", "error") == 0.0


class TestSSEConcurrencyCap:
    # Admission control is the guard against stream pile-up taking a process
    # down; these tests pin the reject/admit boundary and that capacity is
    # released when streams end.

    def test_over_cap_rejects_with_503_and_jittered_retry_after(self):
        def endless() -> Iterator[bytes]:
            while True:
                yield b": ping\n\n"

        with override_settings(SSE_MAX_CONCURRENT_STREAMS_PER_PROCESS=1):
            admitted = sse_streaming_response(endless(), endpoint="test_cap")
            assert isinstance(admitted, StreamingHttpResponse)
            next(_sync_content(admitted))  # occupy the only slot
            try:
                rejected = sse_streaming_response(_gen(), endpoint="test_cap")
                assert rejected.status_code == HTTPStatus.SERVICE_UNAVAILABLE
                assert not isinstance(rejected, StreamingHttpResponse)
                assert 15 <= int(rejected.headers["Retry-After"]) < 45
            finally:
                # Always release the slot: a failed assertion must not leak the
                # active-stream count into other tests.
                admitted.close()

    def test_capacity_frees_up_when_a_stream_closes(self):
        def endless() -> Iterator[bytes]:
            while True:
                yield b": ping\n\n"

        with override_settings(SSE_MAX_CONCURRENT_STREAMS_PER_PROCESS=1):
            first = sse_streaming_response(endless(), endpoint="test_cap_release")
            assert isinstance(first, StreamingHttpResponse)
            next(_sync_content(first))
            first.close()
            second = sse_streaming_response(_gen(), endpoint="test_cap_release")
            assert isinstance(second, StreamingHttpResponse)
            assert b"".join(_sync_content(second)) == b"data: hello\n\n"

    def test_cap_of_zero_rejects_everything(self):
        with override_settings(SSE_MAX_CONCURRENT_STREAMS_PER_PROCESS=0):
            rejected = sse_streaming_response(_gen(), endpoint="test_cap_zero")
            assert rejected.status_code == HTTPStatus.SERVICE_UNAVAILABLE
            rejected_count = REGISTRY.get_sample_value(
                "posthog_sse_rejected_over_cap_total", {"endpoint": "test_cap_zero"}
            )
            assert rejected_count is not None and rejected_count >= 1.0


class TestSSEKillswitch:
    # The killswitch is the only server-side signal that reaches already-open
    # EventSource clients (204 => CLOSED, no reconnect). These tests pin that a
    # kill never opens the stream and that flag-evaluation failures fail open.

    def test_killswitch_on_returns_204_without_opening_the_stream(self):
        with mock.patch("posthog.api.streaming.posthoganalytics.feature_enabled", return_value=True) as feature_enabled:
            response = sse_streaming_response(_gen(), endpoint="test_kill_on", killswitch_flag="test-sse-killswitch")
        assert response.status_code == HTTPStatus.NO_CONTENT
        assert not isinstance(response, StreamingHttpResponse)
        assert _open_connections("test_kill_on") == 0.0
        assert REGISTRY.get_sample_value("posthog_sse_killswitch_rejected_total", {"endpoint": "test_kill_on"}) == 1.0
        feature_enabled.assert_called_once_with(
            "test-sse-killswitch",
            "sse-killswitch",
            only_evaluate_locally=True,
            send_feature_flag_events=False,
        )

    def test_killswitch_off_streams_normally(self):
        with mock.patch("posthog.api.streaming.posthoganalytics.feature_enabled", return_value=False):
            response = sse_streaming_response(_gen(), endpoint="test_kill_off", killswitch_flag="test-sse-killswitch")
        assert isinstance(response, StreamingHttpResponse)
        assert b"".join(_sync_content(response)) == b"data: hello\n\n"

    def test_flag_evaluation_error_fails_open(self):
        with mock.patch(
            "posthog.api.streaming.posthoganalytics.feature_enabled", side_effect=RuntimeError("flags down")
        ):
            response = sse_streaming_response(_gen(), endpoint="test_kill_err", killswitch_flag="test-sse-killswitch")
        assert isinstance(response, StreamingHttpResponse)
        assert b"".join(_sync_content(response)) == b"data: hello\n\n"
