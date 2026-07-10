from products.warehouse_sources.backend.temporal.data_imports.sources.common.canonical_descriptions import (
    CanonicalDescriptions,
)

_DOCS = "https://docs.together.ai/reference"

# Descriptions taken from Together AI's public API reference (https://docs.together.ai/reference).
# Partial coverage is fine — any endpoint/column not listed here falls back to LLM enrichment.
CANONICAL_DESCRIPTIONS: CanonicalDescriptions = {
    "fine_tunes": {
        "description": "Fine-tuning jobs run on the account, with their hyperparameters, source files, and lifecycle status.",
        "docs_url": f"{_DOCS}/get-fine-tunes",
        "columns": {
            "id": "Unique identifier for the fine-tuning job (e.g. ft-...).",
            "status": "Current job status (pending, queued, running, compressing, uploading, cancel_requested, cancelled, error, completed).",
            "created_at": "Timestamp when the job was created.",
            "updated_at": "Timestamp when the job was last updated.",
            "user_id": "ID of the user that owns the job.",
            "model": "Base model being fine-tuned.",
            "model_output_name": "Name of the resulting fine-tuned model.",
            "training_file": "File ID of the training dataset.",
            "validation_file": "File ID of the validation dataset.",
            "training_type": "Fine-tuning type (full or LoRA).",
            "training_method": "Training method (SFT or DPO).",
            "n_epochs": "Number of training epochs.",
            "n_checkpoints": "Number of checkpoints saved during training.",
            "batch_size": "Training batch size.",
            "learning_rate": "Training learning rate.",
            "total_price": "Total price charged for the job.",
            "token_count": "Number of tokens processed during training.",
        },
    },
    "batches": {
        "description": "Batch inference jobs submitted to the OpenAI-compatible batch API for bulk request processing.",
        "docs_url": f"{_DOCS}/batch-list",
        "columns": {
            "id": "Unique identifier for the batch job.",
            "user_id": "ID of the user that owns the batch job.",
            "input_file_id": "File ID of the uploaded batch input file.",
            "file_size_bytes": "Size of the input file in bytes.",
            "status": "Batch status (VALIDATING, IN_PROGRESS, COMPLETED, FAILED, EXPIRED, CANCELLED).",
            "job_deadline": "Deadline by which the batch must complete.",
            "created_at": "Timestamp when the batch job was created.",
            "completed_at": "Timestamp when the batch job completed.",
            "endpoint": "Inference endpoint the batch targets (e.g. /v1/chat/completions).",
            "progress": "Completion progress from 0.0 to 100.",
            "model_id": "Model used to process the batch requests.",
            "output_file_id": "File ID of the batch output.",
            "error_file_id": "File ID of the batch errors.",
            "error": "Error message if the batch failed.",
        },
    },
    "files": {
        "description": "Files uploaded to Together AI for fine-tuning, evaluations, and the batch API.",
        "docs_url": f"{_DOCS}/get-files",
        "columns": {
            "id": "Unique identifier for the file (e.g. file-...).",
            "object": "Object type, always 'file'.",
            "created_at": "Unix timestamp when the file was created.",
            "filename": "Name of the file as it was uploaded.",
            "bytes": "Number of bytes in the file.",
            "purpose": "What the file is used for (fine-tune, eval, batch-api).",
            "processing_status": "Validation pipeline state (PENDING, QUEUED, RUNNING, COMPLETED, FAILED, INVALID_FORMAT).",
        },
    },
    "endpoints": {
        "description": "Dedicated and serverless model endpoints deployed on the account.",
        "docs_url": f"{_DOCS}/listendpoints",
        "columns": {
            "id": "Unique identifier for the endpoint (e.g. endpoint-...).",
            "object": "Object type, always 'endpoint'.",
            "name": "System name for the endpoint.",
            "model": "The model deployed on this endpoint.",
            "type": "Endpoint type (serverless or dedicated).",
            "owner": "The owner of this endpoint.",
            "state": "Endpoint state (PENDING, STARTING, STARTED, STOPPING, STOPPED, ERROR).",
            "created_at": "Timestamp when the endpoint was created.",
        },
    },
    "evaluations": {
        "description": "Evaluation jobs (classify, score, or compare) run against model outputs.",
        "docs_url": f"{_DOCS}/list-evaluations",
        "columns": {
            "workflow_id": "The evaluation job ID.",
            "created_at": "Timestamp when the evaluation job was created.",
            "updated_at": "Timestamp when the evaluation job was last updated.",
            "status": "Job status (pending, queued, running, completed, error, user_error).",
            "type": "Evaluation type (classify, score, compare).",
            "parameters": "Parameters the evaluation was configured with.",
            "results": "Evaluation results, varying by evaluation type.",
        },
    },
    "models": {
        "description": "The catalog of models available on Together AI, including type, context length, and pricing.",
        "docs_url": f"{_DOCS}/models",
        "columns": {
            "id": "Unique model identifier (e.g. meta-llama/Llama-3-8b-chat-hf).",
            "object": "Object type, always 'model'.",
            "created": "Unix timestamp when the model was added.",
            "type": "Model type (chat, language, code, image, embedding, moderation, rerank).",
            "display_name": "Human-readable model name.",
            "organization": "Organization that publishes the model.",
            "context_length": "Maximum context length the model supports.",
            "pricing": "Per-token and hourly pricing for the model.",
        },
    },
}
