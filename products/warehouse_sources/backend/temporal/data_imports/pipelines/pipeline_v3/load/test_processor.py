from typing import Any

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import pyarrow as pa
from parameterized import parameterized

from products.warehouse_sources.backend.temporal.data_imports.pipelines.pipeline.consts import PARTITION_KEY
from products.warehouse_sources.backend.temporal.data_imports.pipelines.pipeline_v3.load.processor import (
    _apply_partitioning,
    _get_write_type,
    _mark_job_completed,
    _promote_staged_cursor,
)
from products.warehouse_sources.backend.temporal.data_imports.pipelines.test_mocks import mock_delta_table


@pytest.fixture(autouse=True)
def _no_close_old_connections():
    # These are pure unit tests, but close_old_connections() touches the Django
    # connection, which pytest-django blocks for unmarked tests once a django_db
    # test has run in the same session — an order-dependent failure.
    with patch(
        "products.warehouse_sources.backend.temporal.data_imports.pipelines.pipeline_v3.load.processor.close_old_connections"
    ):
        yield


class TestGetWriteType:
    @parameterized.expand(
        [
            ("incremental", "incremental"),
            ("cdc", "incremental"),
            ("append", "append"),
            ("full_refresh", "full_refresh"),
        ]
    )
    def test_mapping(self, sync_type, expected):
        assert _get_write_type(sync_type) == expected


def _export_signal(**overrides: Any) -> MagicMock:
    signal = MagicMock()
    signal.partition_keys = overrides.get("partition_keys")
    signal.partition_count = overrides.get("partition_count")
    signal.partition_size = overrides.get("partition_size")
    signal.partition_mode = overrides.get("partition_mode")
    signal.partition_format = overrides.get("partition_format")
    return signal


def _schema(**overrides: Any) -> MagicMock:
    schema = MagicMock()
    schema.partitioning_enabled = overrides.get("partitioning_enabled", True)
    schema.partition_mode = overrides.get("partition_mode")
    schema.partition_format = overrides.get("partition_format")
    schema.partitioning_keys = overrides.get("partitioning_keys")
    schema.set_partitioning_enabled = MagicMock()
    return schema


_COL_ID = pa.field("id", pa.int64())
_COL_PARTITION = pa.field(PARTITION_KEY, pa.string())


class TestApplyPartitioning:
    """Regression coverage for the `DeltaError: Specified table partitioning does not match` bug.

    When a delta table contains `_ph_partition_key` in its schema but NOT in its
    partition columns (e.g. left over from a prior write committed with
    `partition_by=None`), the v3 loader must skip partitioning subsequent writes
    to avoid delta-rs rejecting the `partition_by=PARTITION_KEY` argument.
    """

    @parameterized.expand(
        [
            # Column in schema but NOT in partition_columns → skip (the exact bug scenario).
            ("column_in_schema_not_partitioned", [_COL_ID, _COL_PARTITION], [], False),
            # `metadata().partition_columns` returning None → skip defensively.
            ("partition_columns_is_none", [_COL_ID, _COL_PARTITION], None, False),
            # Truly partitioned by `_ph_partition_key` → happy path, partitioning applies.
            ("table_partitioned_by_key", [_COL_ID, _COL_PARTITION], [PARTITION_KEY], True),
        ]
    )
    def test_respects_existing_delta_partition_columns(
        self,
        _case: str,
        schema_fields: list[pa.Field],
        partition_columns: list[str] | None,
        expect_key: bool,
    ):
        pa_table = pa.table({"id": [1, 2, 3]})
        delta_table = mock_delta_table(schema_fields=schema_fields, partition_columns=partition_columns)

        schema_kwargs: dict[str, Any] = {}
        export_kwargs: dict[str, Any] = {"partition_keys": ["id"]}
        if expect_key:
            schema_kwargs.update(
                partitioning_enabled=True,
                partition_mode="md5",
                partitioning_keys=["id"],
            )
            export_kwargs["partition_count"] = 10

        result = _apply_partitioning(
            export_signal=_export_signal(**export_kwargs),
            pa_table=pa_table,
            existing_delta_table=delta_table,
            schema=_schema(**schema_kwargs),
        )

        if expect_key:
            assert PARTITION_KEY in result.column_names
        else:
            assert PARTITION_KEY not in result.column_names
            assert result.equals(pa_table)

    def test_skips_partitioning_when_no_partition_keys(self):
        pa_table = pa.table({"id": [1, 2, 3]})

        result = _apply_partitioning(
            export_signal=_export_signal(partition_keys=None),
            pa_table=pa_table,
            existing_delta_table=None,
            schema=_schema(),
        )

        assert PARTITION_KEY not in result.column_names
        assert result.equals(pa_table)


class TestPromoteStagedCursor:
    def _make_signal(self, **overrides: Any) -> MagicMock:
        signal = MagicMock()
        signal.schema_id = overrides.get("schema_id", "schema-1")
        signal.team_id = overrides.get("team_id", 1)
        signal.run_uuid = overrides.get("run_uuid", "run-abc-a1")
        signal.job_id = overrides.get("job_id", "job-1")
        return signal

    @patch(
        "products.warehouse_sources.backend.temporal.data_imports.pipelines.pipeline_v3.load.processor.ExternalDataSchema.objects"
    )
    def test_promotes_when_run_uuid_matches(self, mock_objects: MagicMock) -> None:
        schema = MagicMock()
        schema.promote_staged_incremental_values.return_value = True
        mock_objects.get.return_value = schema

        signal = self._make_signal()
        _promote_staged_cursor(signal)

        mock_objects.get.assert_called_once_with(id="schema-1", team_id=1)
        schema.promote_staged_incremental_values.assert_called_once_with("run-abc-a1")

    @patch(
        "products.warehouse_sources.backend.temporal.data_imports.pipelines.pipeline_v3.load.processor.ExternalDataSchema.objects"
    )
    def test_does_not_raise_on_promotion_failure(self, mock_objects: MagicMock) -> None:
        schema = MagicMock()
        schema.promote_staged_incremental_values.side_effect = RuntimeError("db error")
        mock_objects.get.return_value = schema

        signal = self._make_signal()
        _promote_staged_cursor(signal)

    @patch(
        "products.warehouse_sources.backend.temporal.data_imports.pipelines.pipeline_v3.load.processor.ExternalDataSchema.objects"
    )
    def test_does_not_raise_when_schema_missing(self, mock_objects: MagicMock) -> None:
        from products.warehouse_sources.backend.models.external_data_schema import ExternalDataSchema

        mock_objects.get.side_effect = ExternalDataSchema.DoesNotExist()
        signal = self._make_signal()
        _promote_staged_cursor(signal)


class TestMarkJobCompleted:
    def _make_signal(self, **overrides: Any) -> MagicMock:
        signal = MagicMock()
        signal.schema_id = overrides.get("schema_id", "schema-1")
        signal.team_id = overrides.get("team_id", 1)
        signal.run_uuid = overrides.get("run_uuid", "run-abc-a1")
        signal.job_id = overrides.get("job_id", "job-1")
        return signal

    @parameterized.expand(
        [
            # If the job was cancelled (absorbing Failed) after the final batch passed
            # should_process_batch, the Completed write is rejected and the staged
            # incremental cursor must NOT be promoted.
            ("completed_write_lands", "Completed", True),
            ("completed_write_absorbed_by_failed", "Failed", False),
        ]
    )
    @patch(
        "products.warehouse_sources.backend.temporal.data_imports.pipelines.pipeline_v3.load.processor.release_v3_pipeline_lock"
    )
    @patch(
        "products.warehouse_sources.backend.temporal.data_imports.pipelines.pipeline_v3.load.processor.ExternalDataJob.objects"
    )
    @patch(
        "products.warehouse_sources.backend.temporal.data_imports.pipelines.pipeline_v3.load.processor.finish_row_tracking",
        new_callable=AsyncMock,
    )
    @patch(
        "products.warehouse_sources.backend.temporal.data_imports.pipelines.pipeline_v3.load.processor.ExternalDataSchema.objects"
    )
    @patch(
        "products.warehouse_sources.backend.temporal.data_imports.pipelines.pipeline_v3.load.processor.update_external_job_status"
    )
    def test_promotes_cursor_only_when_completed_write_lands(
        self,
        _case: str,
        resulting_status: str,
        expect_promotion: bool,
        mock_update: MagicMock,
        mock_schema_objects: MagicMock,
        mock_finish_row_tracking: AsyncMock,
        mock_job_objects: MagicMock,
        mock_release: MagicMock,
    ) -> None:
        model = MagicMock()
        model.status = resulting_status
        mock_update.return_value = model

        schema = MagicMock()
        mock_schema_objects.get.return_value = schema
        job = MagicMock()
        job.workflow_run_id = "wf-run-1"
        mock_job_objects.only.return_value.get.return_value = job

        _mark_job_completed(self._make_signal())

        if expect_promotion:
            schema.promote_staged_incremental_values.assert_called_once_with("run-abc-a1")
        else:
            schema.promote_staged_incremental_values.assert_not_called()
            mock_finish_row_tracking.assert_not_awaited()
        # The pipeline lock must be released either way, or the next sync is blocked.
        mock_release.assert_called_once_with(team_id=1, schema_id="schema-1", token="wf-run-1")
