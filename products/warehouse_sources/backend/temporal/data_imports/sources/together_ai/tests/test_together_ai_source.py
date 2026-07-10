from typing import Any

from unittest.mock import MagicMock, patch

from parameterized import parameterized

from posthog.schema import DataWarehouseSourceCategory, ReleaseStatus, SourceFieldInputConfig

from products.warehouse_sources.backend.temporal.data_imports.sources.together_ai.canonical_descriptions import (
    CANONICAL_DESCRIPTIONS,
)
from products.warehouse_sources.backend.temporal.data_imports.sources.together_ai.settings import (
    ENDPOINTS,
    TOGETHER_AI_ENDPOINTS,
)
from products.warehouse_sources.backend.temporal.data_imports.sources.together_ai.source import TogetherAISource
from products.warehouse_sources.backend.types import ExternalDataSourceType


def _config(api_key: str = "key") -> Any:
    config = MagicMock()
    config.api_key = api_key
    return config


class TestSourceConfig:
    def test_source_type(self) -> None:
        assert TogetherAISource().source_type == ExternalDataSourceType.TOGETHERAI

    def test_config_is_alpha_and_unreleased(self) -> None:
        config = TogetherAISource().get_source_config
        # This source ships behind the unreleased flag while it's still alpha.
        assert config.unreleasedSource is True
        assert config.releaseStatus == ReleaseStatus.ALPHA
        assert config.category == DataWarehouseSourceCategory.ENGINEERING___MONITORING
        assert config.docsUrl == "https://posthog.com/docs/cdp/sources/together-ai"

    def test_fields(self) -> None:
        fields = {
            f.name: f for f in TogetherAISource().get_source_config.fields if isinstance(f, SourceFieldInputConfig)
        }
        assert set(fields) == {"api_key"}
        assert fields["api_key"].required is True
        assert fields["api_key"].secret is True


class TestGetSchemas:
    def test_lists_every_endpoint_as_full_refresh(self) -> None:
        schemas = TogetherAISource().get_schemas(_config(), team_id=1)
        assert {s.name for s in schemas} == set(ENDPOINTS)
        # No endpoint has pagination or a server-side timestamp filter, so nothing is incremental.
        assert all(not s.supports_incremental and not s.supports_append for s in schemas)
        assert all(s.incremental_fields == [] for s in schemas)

    def test_evaluations_key_on_workflow_id(self) -> None:
        schemas = {s.name: s for s in TogetherAISource().get_schemas(_config(), team_id=1)}
        assert schemas["evaluations"].detected_primary_keys == ["workflow_id"]
        assert schemas["fine_tunes"].detected_primary_keys == ["id"]

    def test_models_off_by_default(self) -> None:
        # The global model catalog is the same for everyone and large, so it isn't synced by default.
        schemas = {s.name: s for s in TogetherAISource().get_schemas(_config(), team_id=1)}
        assert schemas["models"].should_sync_default is False
        assert schemas["fine_tunes"].should_sync_default is True

    def test_names_filter(self) -> None:
        schemas = TogetherAISource().get_schemas(_config(), team_id=1, names=["batches", "files"])
        assert {s.name for s in schemas} == {"batches", "files"}

    def test_lists_tables_without_credentials(self) -> None:
        # Static endpoint catalog (no I/O) — public docs render the table list.
        assert TogetherAISource.lists_tables_without_credentials is True
        tables = TogetherAISource().get_documented_tables()
        assert {t["name"] for t in tables} == set(ENDPOINTS)
        fine_tunes = next(t for t in tables if t["name"] == "fine_tunes")
        assert fine_tunes["sync_methods"] == ["Full refresh"]
        assert fine_tunes["primary_keys"] == ["id"]


class TestValidateCredentials:
    def test_success(self) -> None:
        with patch(
            "products.warehouse_sources.backend.temporal.data_imports.sources.together_ai.source.validate_together_ai_credentials",
            return_value=True,
        ) as mocked:
            ok, error = TogetherAISource().validate_credentials(_config(), team_id=1)
        assert ok is True
        assert error is None
        # No schema name at source-create probes the always-reachable /models endpoint.
        mocked.assert_called_once_with("key", "/models")

    def test_failure(self) -> None:
        with patch(
            "products.warehouse_sources.backend.temporal.data_imports.sources.together_ai.source.validate_together_ai_credentials",
            return_value=False,
        ):
            ok, error = TogetherAISource().validate_credentials(_config(), team_id=1)
        assert ok is False
        assert error is not None

    def test_probes_specific_endpoint_path(self) -> None:
        with patch(
            "products.warehouse_sources.backend.temporal.data_imports.sources.together_ai.source.validate_together_ai_credentials",
            return_value=True,
        ) as mocked:
            TogetherAISource().validate_credentials(_config(), team_id=1, schema_name="evaluations")
        mocked.assert_called_once_with("key", "/evaluation")


class TestSourceForPipeline:
    def test_source_for_pipeline_plumbs_arguments(self) -> None:
        inputs = MagicMock()
        inputs.schema_name = "fine_tunes"
        inputs.logger = MagicMock()
        with patch(
            "products.warehouse_sources.backend.temporal.data_imports.sources.together_ai.source.together_ai_source"
        ) as mocked:
            TogetherAISource().source_for_pipeline(_config(), inputs)
        mocked.assert_called_once_with(
            api_key="key",
            endpoint="fine_tunes",
            logger=inputs.logger,
        )


class TestNonRetryableErrors:
    @parameterized.expand(
        [
            (
                "unauthorized",
                "401 Client Error: Unauthorized for url: https://api.together.xyz/v1/fine-tunes",
            ),
            (
                "forbidden",
                "403 Client Error: Forbidden for url: https://api.together.xyz/v1/batches",
            ),
        ]
    )
    def test_credential_errors_are_non_retryable(self, _name: str, observed_error: str) -> None:
        non_retryable = TogetherAISource().get_non_retryable_errors()
        assert any(key in observed_error for key in non_retryable)

    @parameterized.expand(
        [
            ("read_timeout", "HTTPSConnectionPool(host='api.together.xyz', port=443): Read timed out."),
            (
                "server_error",
                "500 Server Error: Internal Server Error for url: https://api.together.xyz/v1/models",
            ),
            ("rate_limited", "429 Client Error: Too Many Requests for url: https://api.together.xyz/v1/files"),
        ]
    )
    def test_transient_errors_remain_retryable(self, _name: str, other_error: str) -> None:
        non_retryable = TogetherAISource().get_non_retryable_errors()
        assert not any(key in other_error for key in non_retryable)


class TestCanonicalDescriptions:
    def test_canonical_descriptions_keys_are_known_endpoints(self) -> None:
        # Every documented table must map to a real endpoint, or its descriptions never apply.
        assert set(CANONICAL_DESCRIPTIONS).issubset(set(ENDPOINTS))

    def test_source_exposes_canonical_descriptions(self) -> None:
        assert TogetherAISource().get_canonical_descriptions() is CANONICAL_DESCRIPTIONS

    def test_every_endpoint_has_descriptions(self) -> None:
        # All shipped endpoints are documented up front from the API reference.
        assert set(CANONICAL_DESCRIPTIONS) == set(TOGETHER_AI_ENDPOINTS)
