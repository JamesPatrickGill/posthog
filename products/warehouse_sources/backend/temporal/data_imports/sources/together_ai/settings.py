from dataclasses import dataclass, field


@dataclass
class TogetherAIEndpointConfig:
    path: str
    # JSON key holding the row array. Together AI is inconsistent: some list endpoints wrap rows in
    # `{"data": [...]}` (fine-tunes, files, endpoints) while others return a bare top-level array
    # (batches, evaluations, models). `None` means the body itself is the array.
    data_key: str | None = "data"
    # Primary key columns for dedup on merge. IDs are globally unique per resource, so a single key
    # column is safe. Evaluations key on `workflow_id` (there is no `id` field).
    primary_keys: list[str] = field(default_factory=lambda: ["id"])
    # Stable creation timestamp used for datetime partitioning (never a churning field like
    # `updated_at`). `None` disables partitioning for endpoints whose only timestamp is an integer
    # Unix value (files `created_at`, models `created`), which the datetime partitioner can't parse.
    partition_key: str | None = "created_at"
    # Whether the table is selected for sync by default in the UI.
    should_sync_default: bool = True


# Together AI's queryable historical entities. Every one is a plain `GET /v1/<resource>` list
# endpoint with Bearer API-key auth. None expose pagination or a server-side timestamp filter, so
# each list returns its full collection in a single response and every table syncs as full refresh
# (see source.py / get_schemas). Collections are small per account, so this is cheap.
TOGETHER_AI_ENDPOINTS: dict[str, TogetherAIEndpointConfig] = {
    # Fine-tuning jobs, with hyperparameters, training/validation files, and lifecycle timestamps.
    "fine_tunes": TogetherAIEndpointConfig(path="/fine-tunes", data_key="data"),
    # Batch inference jobs (OpenAI-compatible bulk request processing).
    "batches": TogetherAIEndpointConfig(path="/batches", data_key=None),
    # Uploaded files used for fine-tuning, evaluations, and the batch API. `created_at` is an integer
    # Unix timestamp, so it can't drive datetime partitioning.
    "files": TogetherAIEndpointConfig(path="/files", data_key="data", partition_key=None),
    # Dedicated and serverless model endpoints deployed on the account.
    "endpoints": TogetherAIEndpointConfig(path="/endpoints", data_key="data"),
    # Evaluation jobs (classify / score / compare). The job identifier is `workflow_id`, not `id`.
    "evaluations": TogetherAIEndpointConfig(path="/evaluation", data_key=None, primary_keys=["workflow_id"]),
    # The global model catalog. Same for every account and large, so it's off by default; `created`
    # is an integer Unix timestamp, so no datetime partitioning.
    "models": TogetherAIEndpointConfig(path="/models", data_key=None, partition_key=None, should_sync_default=False),
}

ENDPOINTS = tuple(TOGETHER_AI_ENDPOINTS.keys())
