from typing import Any

import pytest
from unittest.mock import MagicMock, patch

import requests
from parameterized import parameterized

from products.warehouse_sources.backend.temporal.data_imports.sources.together_ai import together_ai
from products.warehouse_sources.backend.temporal.data_imports.sources.together_ai.settings import (
    ENDPOINTS,
    TOGETHER_AI_ENDPOINTS,
)
from products.warehouse_sources.backend.temporal.data_imports.sources.together_ai.together_ai import (
    TogetherAIRetryableError,
    _build_url,
    get_rows,
    together_ai_source,
    validate_credentials,
)


def _response_with_status(status_code: int, body: bytes = b"") -> requests.Response:
    response = requests.Response()
    response.status_code = status_code
    response._content = body
    return response


class TestBuildUrl:
    def test_no_params(self) -> None:
        assert _build_url("/fine-tunes") == "https://api.together.xyz/v1/fine-tunes"

    def test_omits_none_and_empty_params(self) -> None:
        url = _build_url("/evaluation", {"status": None, "limit": ""})
        assert url == "https://api.together.xyz/v1/evaluation"

    def test_includes_set_params(self) -> None:
        url = _build_url("/evaluation", {"limit": 10})
        assert url == "https://api.together.xyz/v1/evaluation?limit=10"


class TestFetch:
    @parameterized.expand([("rate_limited", 429), ("server_error", 500), ("bad_gateway", 503)])
    def test_retryable_statuses_raise_retryable_error(self, _name: str, status: int) -> None:
        session = MagicMock()
        session.get.return_value = _response_with_status(status)
        # No-op the backoff sleep so the 5 attempts run instantly.
        with patch.object(together_ai._fetch.retry, "sleep", lambda *a, **k: None):  # type: ignore[attr-defined]
            with pytest.raises(TogetherAIRetryableError):
                together_ai._fetch(session, "https://api.together.xyz/v1/fine-tunes", "data", MagicMock())
        assert session.get.call_count == 5

    @parameterized.expand([("unauthorized", 401), ("forbidden", 403), ("not_found", 404)])
    def test_client_errors_raise_http_error(self, _name: str, status: int) -> None:
        session = MagicMock()
        session.get.return_value = _response_with_status(status)
        with pytest.raises(requests.HTTPError):
            together_ai._fetch(session, "https://api.together.xyz/v1/fine-tunes", "data", MagicMock())

    def test_unwraps_data_key(self) -> None:
        # fine-tunes / files / endpoints wrap rows in {"data": [...]}.
        session = MagicMock()
        session.get.return_value = _response_with_status(200, b'{"object": "list", "data": [{"id": "ft-1"}]}')
        rows = together_ai._fetch(session, "https://api.together.xyz/v1/fine-tunes", "data", MagicMock())
        assert rows == [{"id": "ft-1"}]

    def test_missing_data_key_raises_value_error(self) -> None:
        # A wrapped payload missing its data key must fault, not default to []: these tables full-refresh,
        # so silently treating a changed shape as empty would wipe previously synced rows. Bypass retries.
        session = MagicMock()
        session.get.return_value = _response_with_status(200, b'{"object": "list"}')
        with pytest.raises(ValueError):
            together_ai._fetch(session, "https://api.together.xyz/v1/files", "data", MagicMock())
        assert session.get.call_count == 1

    def test_empty_data_array_yields_empty(self) -> None:
        # A wrapped payload whose data key is present but empty (e.g. empty account) is a valid empty collection.
        session = MagicMock()
        session.get.return_value = _response_with_status(200, b'{"object": "list", "data": []}')
        rows = together_ai._fetch(session, "https://api.together.xyz/v1/files", "data", MagicMock())
        assert rows == []

    def test_bare_array_response(self) -> None:
        # batches / evaluations / models return a bare top-level array.
        session = MagicMock()
        session.get.return_value = _response_with_status(200, b'[{"id": "batch-1"}, {"id": "batch-2"}]')
        rows = together_ai._fetch(session, "https://api.together.xyz/v1/batches", None, MagicMock())
        assert rows == [{"id": "batch-1"}, {"id": "batch-2"}]

    def test_unexpected_shape_raises_value_error(self) -> None:
        # A bare-array endpoint returning an object is a permanent contract violation; bypass retries.
        session = MagicMock()
        session.get.return_value = _response_with_status(200, b'{"error": "unexpected"}')
        with pytest.raises(ValueError):
            together_ai._fetch(session, "https://api.together.xyz/v1/batches", None, MagicMock())
        assert session.get.call_count == 1


class TestValidateCredentials:
    def test_returns_true_on_200(self) -> None:
        session = MagicMock()
        session.get.return_value = _response_with_status(200)
        with patch.object(together_ai, "_make_session", return_value=session):
            assert validate_credentials("key") is True
        # Defaults to probing /models.
        assert session.get.call_args.args[0] == "https://api.together.xyz/v1/models"

    def test_returns_false_on_401(self) -> None:
        session = MagicMock()
        session.get.return_value = _response_with_status(401)
        with patch.object(together_ai, "_make_session", return_value=session):
            assert validate_credentials("key", path="/fine-tunes") is False

    def test_returns_false_on_exception(self) -> None:
        session = MagicMock()
        session.get.side_effect = requests.ConnectionError()
        with patch.object(together_ai, "_make_session", return_value=session):
            assert validate_credentials("key") is False


class TestGetRows:
    def _collect(self, monkeypatch: Any, endpoint: str, rows: list[dict]) -> tuple[list[dict], list[tuple[str, Any]]]:
        fetched: list[tuple[str, Any]] = []

        def fake_fetch(session: Any, url: str, data_key: Any, logger: Any) -> list[dict]:
            fetched.append((url, data_key))
            return rows

        monkeypatch.setattr(together_ai, "_fetch", fake_fetch)
        monkeypatch.setattr(together_ai, "make_tracked_session", lambda **kwargs: MagicMock())

        collected: list[dict] = []
        for page in get_rows(api_key="key", endpoint=endpoint, logger=MagicMock()):
            collected.extend(page)
        return collected, fetched

    def test_yields_full_collection(self, monkeypatch: Any) -> None:
        rows, fetched = self._collect(monkeypatch, "fine_tunes", [{"id": "ft-1"}, {"id": "ft-2"}])
        assert rows == [{"id": "ft-1"}, {"id": "ft-2"}]
        # A single request per endpoint (no pagination), hitting the configured path + data_key.
        assert fetched == [("https://api.together.xyz/v1/fine-tunes", "data")]

    def test_empty_collection_yields_nothing(self, monkeypatch: Any) -> None:
        rows, fetched = self._collect(monkeypatch, "batches", [])
        assert rows == []
        assert fetched == [("https://api.together.xyz/v1/batches", None)]

    def test_evaluations_bare_array_path(self, monkeypatch: Any) -> None:
        _, fetched = self._collect(monkeypatch, "evaluations", [{"workflow_id": "wf-1"}])
        assert fetched == [("https://api.together.xyz/v1/evaluation", None)]


class TestTogetherAISourceResponse:
    @parameterized.expand([(name,) for name in ENDPOINTS])
    def test_source_response_shape(self, name: str) -> None:
        config = TOGETHER_AI_ENDPOINTS[name]
        response = together_ai_source(api_key="key", endpoint=name, logger=MagicMock())
        assert response.name == name
        assert response.primary_keys == config.primary_keys
        assert response.sort_mode == "asc"
        if config.partition_key:
            assert response.partition_keys == [config.partition_key]
            assert response.partition_mode == "datetime"
        else:
            assert response.partition_keys is None
            assert response.partition_mode is None

    def test_no_endpoint_partitions_on_a_churning_field(self) -> None:
        # Guards against accidentally partitioning on a field that changes after creation.
        churning = {"updated_at", "modified_at", "last_seen", "completed_at"}
        assert all(cfg.partition_key not in churning for cfg in TOGETHER_AI_ENDPOINTS.values())

    def test_integer_timestamp_endpoints_are_unpartitioned(self) -> None:
        # files.created_at and models.created are integer Unix values the datetime partitioner can't parse.
        assert TOGETHER_AI_ENDPOINTS["files"].partition_key is None
        assert TOGETHER_AI_ENDPOINTS["models"].partition_key is None
