from collections.abc import Iterator
from typing import Any
from urllib.parse import urlencode

import requests
from structlog.types import FilteringBoundLogger
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter
from urllib3.util.retry import Retry

from products.warehouse_sources.backend.temporal.data_imports.pipelines.pipeline.typings import SourceResponse
from products.warehouse_sources.backend.temporal.data_imports.sources.common.http import make_tracked_session
from products.warehouse_sources.backend.temporal.data_imports.sources.together_ai.settings import TOGETHER_AI_ENDPOINTS

TOGETHER_AI_BASE_URL = "https://api.together.xyz/v1"
REQUEST_TIMEOUT_SECONDS = 60


class TogetherAIRetryableError(Exception):
    pass


def _get_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }


def _make_session(api_key: str) -> requests.Session:
    # `redact_values` masks the bearer token in logged URLs and captured HTTP samples so a failed or
    # sampled request can never persist the raw Together AI credential in PostHog's HTTP telemetry.
    # `retry=Retry(total=0)` disables the session's built-in retry layer: `_fetch` already retries
    # 429/5xx (via `TogetherAIRetryableError`) and timeouts/connection errors through tenacity, so
    # keeping the default urllib3 retries would stack a second layer and multiply requests against a
    # rate-limited endpoint instead of backing off cleanly.
    return make_tracked_session(headers=_get_headers(api_key), redact_values=(api_key,), retry=Retry(total=0))


def _build_url(path: str, params: dict[str, Any] | None = None) -> str:
    query = {key: value for key, value in (params or {}).items() if value is not None and value != ""}
    if not query:
        return f"{TOGETHER_AI_BASE_URL}{path}"
    return f"{TOGETHER_AI_BASE_URL}{path}?{urlencode(query)}"


@retry(
    retry=retry_if_exception_type((TogetherAIRetryableError, requests.ReadTimeout, requests.ConnectionError)),
    stop=stop_after_attempt(5),
    wait=wait_exponential_jitter(initial=1, max=30),
    reraise=True,
)
def _fetch(
    session: requests.Session, url: str, data_key: str | None, logger: FilteringBoundLogger
) -> list[dict[str, Any]]:
    response = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)

    if response.status_code == 429 or response.status_code >= 500:
        raise TogetherAIRetryableError(f"Together AI API error (retryable): status={response.status_code}, url={url}")

    if not response.ok:
        # Don't log the response body: it can echo back the Authorization header or other secrets.
        logger.error(f"Together AI API error: status={response.status_code}, url={url}")
        response.raise_for_status()

    payload = response.json()
    # A shape mismatch (wrapped where we expected bare, proxy HTML, a status-200 error envelope, …) is
    # a permanent API-contract violation, not a transient failure — raise a plain ValueError so it
    # surfaces immediately instead of burning the retry budget on something retries can't fix. Crucially,
    # a missing wrapper key must fault rather than default to `[]`: these tables sync with full refresh,
    # so silently treating a changed shape as an empty collection would wipe previously synced rows.
    if data_key:
        # Some endpoints wrap rows in `{"data": [...]}` (see settings.py).
        if not isinstance(payload, dict) or data_key not in payload:
            raise ValueError(f"Together AI API returned an unexpected response shape: url={url}")
        rows = payload[data_key]
    else:
        # Others return a bare array.
        rows = payload
    if not isinstance(rows, list):
        raise ValueError(f"Together AI API returned an unexpected response shape: url={url}")
    return rows


def validate_credentials(api_key: str, path: str = "/models") -> bool:
    # Together AI keys are account-wide with no per-endpoint scopes, so any list endpoint confirms
    # the token. `/models` is always reachable for a valid key regardless of which features the
    # account uses.
    url = _build_url(path)
    try:
        response = _make_session(api_key).get(url, timeout=10)
        return response.status_code == 200
    except Exception:
        return False


def get_rows(
    api_key: str,
    endpoint: str,
    logger: FilteringBoundLogger,
) -> Iterator[list[dict[str, Any]]]:
    config = TOGETHER_AI_ENDPOINTS[endpoint]
    session = _make_session(api_key)

    # No endpoint paginates, so the whole collection arrives in one response; the pipeline batches it.
    rows = _fetch(session, _build_url(config.path), config.data_key, logger)
    if rows:
        yield rows


def together_ai_source(
    api_key: str,
    endpoint: str,
    logger: FilteringBoundLogger,
) -> SourceResponse:
    config = TOGETHER_AI_ENDPOINTS[endpoint]

    return SourceResponse(
        name=endpoint,
        items=lambda: get_rows(api_key=api_key, endpoint=endpoint, logger=logger),
        primary_keys=config.primary_keys,
        partition_count=1,
        partition_size=1,
        partition_mode="datetime" if config.partition_key else None,
        partition_format="month" if config.partition_key else None,
        partition_keys=[config.partition_key] if config.partition_key else None,
        sort_mode="asc",
    )
