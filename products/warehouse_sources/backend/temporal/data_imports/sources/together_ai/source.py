from typing import Optional, cast

from posthog.schema import (
    DataWarehouseSourceCategory,
    ExternalDataSourceType as SchemaExternalDataSourceType,
    ReleaseStatus,
    SourceConfig,
    SourceFieldInputConfig,
    SourceFieldInputConfigType,
)

from products.warehouse_sources.backend.temporal.data_imports.pipelines.pipeline.typings import (
    SourceInputs,
    SourceResponse,
)
from products.warehouse_sources.backend.temporal.data_imports.sources.common.base import FieldType, SimpleSource
from products.warehouse_sources.backend.temporal.data_imports.sources.common.canonical_descriptions import (
    CanonicalDescriptions,
)
from products.warehouse_sources.backend.temporal.data_imports.sources.common.registry import SourceRegistry
from products.warehouse_sources.backend.temporal.data_imports.sources.common.schema import SourceSchema
from products.warehouse_sources.backend.temporal.data_imports.sources.generated_configs import TogetherAISourceConfig
from products.warehouse_sources.backend.temporal.data_imports.sources.together_ai.settings import (
    ENDPOINTS,
    TOGETHER_AI_ENDPOINTS,
)
from products.warehouse_sources.backend.temporal.data_imports.sources.together_ai.together_ai import (
    together_ai_source,
    validate_credentials as validate_together_ai_credentials,
)
from products.warehouse_sources.backend.types import ExternalDataSourceType


@SourceRegistry.register
class TogetherAISource(SimpleSource[TogetherAISourceConfig]):
    lists_tables_without_credentials = True  # static endpoint catalog — safe for public docs

    @property
    def source_type(self) -> ExternalDataSourceType:
        return ExternalDataSourceType.TOGETHERAI

    @property
    def get_source_config(self) -> SourceConfig:
        return SourceConfig(
            name=SchemaExternalDataSourceType.TOGETHER_AI,
            category=DataWarehouseSourceCategory.ENGINEERING___MONITORING,
            label="Together AI",
            releaseStatus=ReleaseStatus.ALPHA,
            unreleasedSource=True,
            caption="""Enter your Together AI API key to sync your fine-tuning, batch inference, and platform data into the PostHog Data warehouse.

Create an API key in your [Together AI settings](https://api.together.xyz/settings/api-keys), then paste it here. The key is account-wide, so no extra scopes are required.""",
            iconPath="/static/services/together_ai.png",
            docsUrl="https://posthog.com/docs/cdp/sources/together-ai",
            keywords=["ai", "llm", "inference", "fine-tuning"],
            fields=cast(
                list[FieldType],
                [
                    SourceFieldInputConfig(
                        name="api_key",
                        label="API key",
                        type=SourceFieldInputConfigType.PASSWORD,
                        required=True,
                        placeholder="",
                        secret=True,
                    ),
                ],
            ),
        )

    def get_canonical_descriptions(self) -> CanonicalDescriptions:
        from products.warehouse_sources.backend.temporal.data_imports.sources.together_ai.canonical_descriptions import (
            CANONICAL_DESCRIPTIONS,
        )

        return CANONICAL_DESCRIPTIONS

    def get_non_retryable_errors(self) -> dict[str, str | None]:
        return {
            # An invalid or revoked API key surfaces as a requests HTTPError when `_fetch` calls
            # `raise_for_status()`. Retrying can never satisfy a credential problem, so stop the sync.
            # Match the stable status text and base host, not the per-request path/query.
            "401 Client Error: Unauthorized for url: https://api.together.xyz": "Your Together AI API key is invalid or has been revoked. Create a new key in your Together AI settings, then reconnect.",
            "403 Client Error: Forbidden for url: https://api.together.xyz": "Your Together AI API key does not have permission to read this data. Check the key in your Together AI settings, then reconnect.",
        }

    def get_schemas(
        self,
        config: TogetherAISourceConfig,
        team_id: int,
        with_counts: bool = False,
        names: list[str] | None = None,
        force_refresh: bool = False,
    ) -> list[SourceSchema]:
        # Together AI exposes no pagination or server-side timestamp filter on any list endpoint, so
        # every table is full refresh — incremental would re-fetch the whole collection each sync anyway.
        schemas = [
            SourceSchema(
                name=endpoint,
                supports_incremental=False,
                supports_append=False,
                incremental_fields=[],
                detected_primary_keys=TOGETHER_AI_ENDPOINTS[endpoint].primary_keys,
                should_sync_default=TOGETHER_AI_ENDPOINTS[endpoint].should_sync_default,
            )
            for endpoint in ENDPOINTS
        ]
        if names is not None:
            names_set = set(names)
            schemas = [s for s in schemas if s.name in names_set]
        return schemas

    def validate_credentials(
        self, config: TogetherAISourceConfig, team_id: int, schema_name: Optional[str] = None
    ) -> tuple[bool, str | None]:
        path = TOGETHER_AI_ENDPOINTS[schema_name].path if schema_name in TOGETHER_AI_ENDPOINTS else "/models"
        if validate_together_ai_credentials(config.api_key, path):
            return True, None

        return False, "Invalid Together AI API key"

    def source_for_pipeline(self, config: TogetherAISourceConfig, inputs: SourceInputs) -> SourceResponse:
        return together_ai_source(
            api_key=config.api_key,
            endpoint=inputs.schema_name,
            logger=inputs.logger,
        )
