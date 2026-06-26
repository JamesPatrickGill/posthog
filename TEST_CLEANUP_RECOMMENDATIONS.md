# Test-suite cleanup recommendations

Monorepo-wide audit of low-value tests in `PostHog/posthog`, ranked by impact.
Impact has two axes and both matter: **CI wall-time saved** (the primary lever — a few expensive tests often beat hundreds of cheap ones) and **tests/lines removed** (less surface to maintain). They sometimes conflict; this report optimizes for impact, not raw test count.

The bar a test must clear (from [`.agents/skills/writing-tests`](.agents/skills/writing-tests/SKILL.md)): it earns its place only if it catches a realistic regression, through the public interface, that no other test already catches. The recurring failure modes worth removing are trivial/framework behavior, change-detector tests (asserting mock choreography), redundant coverage, coverage-chasing, and cross-language source-scraping.

## How this was produced

- A pre-vetted backend worklist (`posthog/` tree, 161 findings) was treated as a head start and **re-validated against current code**, not taken as gospel.
- The rest of the monorepo (`products/`, `frontend/`, `nodejs/`, `rust/`, `ee/`, `tools/`, `common/`, `services/`) was assessed from scratch via parallel discovery, then findings were spot-checked by hand.
- Every destructive recommendation was **adversarially re-checked**: before removing a test, try to name the regression it would catch; if you can, keep it. This caught real over-flags (below).

### Two over-flags caught during validation — read before acting on the big-count items

The discovery passes over-flagged in two high-count areas. Both would have caused wrong deletions, which cost more than a missed cleanup:

1. **`nodejs/src/ingestion/framework/docs/`** — pitched as ~65 fully-redundant "tutorial" tests. But `docs/02-batch-pipelines.test.ts` uniquely covers the batch **cardinality-mismatch-throws** path (`rejects.toThrow(/different number of results than input values/)`), which the named sibling `base-batch-pipeline.test.ts` does **not** test. These files need per-test triage, and unique edges must be **moved** into the proper unit test files, not deleted. Not shipped.
2. **`products/warehouse_sources/**/test_*_source.py`** — pitched as ~635 templated tests removable by codemod. But `test_single_secret_api_key_field` guards `field.secret is True` (a real **API-key-exposure** regression nothing else catches), and `test_get_source_config` pins user-facing source-catalog metadata (category/release status). A blind codemod would delete real coverage. Only the `test_source_type` constant-echoes and the `source_for_pipeline` plumbing change-detectors are safely removable — and the plumbing removal must preserve the `db_incremental_field_last_value` drop branch. Not shipped as a blind codemod.

### Environment note on verification

This audit ran in a sandbox where the backend's git-sourced Python deps can't be installed and the full service stack isn't up, so backend/jest suites couldn't be executed locally. Every shipped change was verified statically (AST re-parse, reference/​import-usage checks, source tracing for behavior-preserving changes) and is opened as a **draft PR so CI runs the suite** as the final green gate. Wall-time estimates are reasoned from the cost ladder (pure unit → kea logic → Django `TestCase` → ClickHouse-backed → Temporal/real-infra → Playwright e2e), not measured here.

---

## Shipped: 5 draft PRs (top of the ranking)

These were chosen for impact **and** airtight safety (deletion/behavior-preserving, statically verifiable, scoped to one owning team).

| # | PR | Area / team | Action | Tests | Lines | CI wall-time | Confidence |
|---|----|-------------|--------|-------|-------|--------------|------------|
| 1 | [#66335](https://github.com/PostHog/posthog/pull/66335) | Session replay (frontend) — Replay | DELETE 3 oversized snapshots | 1 | ~66,040 | High (serialization/diff of the largest snapshot in the frontend suite, every run) | High |
| 2 | [#66336](https://github.com/PostHog/posthog/pull/66336) | OAuth + scopes models — Security | DELETE trivial/framework tests | 17 | 263 | Low–med (~12 Django `TestCase` setups) | High |
| 3 | [#66337](https://github.com/PostHog/posthog/pull/66337) | Sharing configuration model — Product analytics | DELETE generated-model + ORM round-trip tests | 10 | 104 | Low–med | High |
| 4 | [#66338](https://github.com/PostHog/posthog/pull/66338) | Tasks agent-proxy callback — PostHog Code | FIX `TransactionTestCase`→`TestCase` | 0 | 2 | **High** (15 per-test full-DB flushes → rollbacks) | High |
| 5 | [#66339](https://github.com/PostHog/posthog/pull/66339) | Temporal common servers — Platform | DELETE framework/redundant tests | 5 | 79 | Low–med (removes a real `asyncio.sleep(0.7)` + framework cases) | High |

PR #1 is the single largest line/CI win shipped (one redundant `toMatchSnapshot()` backed a 63,358-line snapshot). PR #4 is the single largest pure CI-wall-time win that's also airtight: the handler dispatches synchronously (no `transaction.on_commit`), so `TestCase` is equivalent.

---

## Full ranked backlog (whole monorepo)

Grouped by area, ordered by impact within each. "Status" flags what's shipped vs. recommended-but-not-shipped (with why).

### Highest CI-wall-time opportunities (not yet shipped)

These are the biggest second-by-second wins. Most are **TRIM-MATRIX / FIX**, which need more care than deletions (and ideally a local run), so they're left for the owning teams.

| Area / file | Owner | Action | Tests | Lines | CI wall-time | Conf | Notes |
|---|---|---|---|---|---|---|---|
| `products/batch_exports/.../destinations/postgres/test_workflow.py` | Batch exports | TRIM-MATRIX (`integration` axis) | ~24–28 combos | — | **Very high** (Temporal worker + ClickHouse + real Postgres per combo) | High | `integration` only swaps where conn params come from; data path identical per model |
| `products/batch_exports/.../destinations/bigquery/test_workflow.py` | Batch exports | TRIM-MATRIX (`interval`×`use_json_type`×`exclude_events`) | ~20 combos | — | **Very high** (Temporal + ClickHouse + real BigQuery) | High | `interval` doesn't change the BQ write path |
| `products/batch_exports/.../destinations/s3/test_workflow_with_minio_bucket.py` | Batch exports | TRIM-MATRIX (4 intervals → 1) | ~21 combos | — | **Very high** (Temporal + ClickHouse + object store) | High | interval→table is unit-tested in `test_internal_stage` |
| `products/batch_exports/.../destinations/snowflake/test_workflow_e2e.py` | Batch exports | TRIM (real-Snowflake combos → 1 smoke + real-only) | ~8 | — | **Very high** (real Snowflake round-trips) | Med | confirm mocked twin `test_workflow.py` asserts record contents |
| `ee/billing/test/test_quota_limiting.py` | Billing | FIX 7× `time.sleep(1)` → `flush_persons_and_events()` | 0 | — | **High** (~7s/run + flake; ClickHouse-backed) | High | repo already uses the flush helper for this |
| `products/marketing_analytics/.../test_conversion_goal_processor.py` | Marketing analytics | TRIM-MATRIX temporal-attribution cluster | ~12–14 → ~6 | ~900–1100 | **High** (`execute_hogql_query` per test) | Med | keep first/last-touch (different attribution path) |
| `products/web_analytics/.../test_weekly_digest_workflows.py`, `test_digest_notification_workflows.py` | Web analytics | TRIM large-org concurrency cases | 2 | — | **High** (real `WorkflowEnvironment` + Worker) | High | 50-org case already exercises the semaphore |
| `products/experiments/.../test_mean_metric.py` | Experiments | MERGE MAX/MIN/AVG → 1 parameterized | ~4 | ~150 | **High** (`@snapshot_clickhouse_queries` ×2 each) | Med | keep `use_precomputation` axis (real path split) |
| `nodejs/.../groups/repositories/postgres-group-repository.integration.test.ts` | Ingestion (persons/groups) | MERGE CRUD/option permutations → `it.each` | ~30 | ~500 | **High** (fresh hub+PG+Redis per test) | High | keep race/constraint/type-index tests |
| `nodejs/src/cdp/workflows-e2e.test.ts` | CDP | FIX hardcoded `setTimeout(…,1000)` → poll on state | 0 | — | High (real PG + multi-second sleeps) | High | keep the e2e assertions; deflake the waits |
| `nodejs/src/common/kafka/consumer/consumer-v2.test.ts` (+ v1) | Ingestion (kafka) | FIX `delay()` → fake timers/microtask flush | 0 | — | Med (~10–15s/run; fully mocked) | High | **keep `consumer-v1.test.ts`** — v1 is live behind `CONSUMER_USE_V2` |
| `rust/feature-flags/tests/test_rate_limiting.rs` | Feature flags | MERGE token/IP `#[rstest]` | ~8 | ~400 | **High** (real Redis + sleeps, O(n²) matrix) | Med-high | |
| `rust/capture/tests/header_timeout.rs` | Capture | DELETE (whole file) | 3 | 165 | **High** (real server ×3 + 1.25s sleeps, runs every CI) | High | validates Axum's header-timeout config, not our logic |
| `rust/capture` mock `realistic_*` sink tests vs `tests/v1_sink_integration.rs` | Capture | MERGE (drop mock round-trip twins) | ~6–8 | ~150–200 | Med (same scenarios run twice) | High | keep mock-only error-injection/partition-key tests |
| `rust/feature-flags/tests/test_flags.rs` v1/v2 twins | Feature flags | MERGE 3 pairs via `#[rstest]` version cases | 3 | ~265 | Med (full request flow, real PG/Redis) | High | |
| `products/conversations/backend/temporal/tests/test_pipeline.py` arg-plumbing | Conversations | MERGE (assert-positional-arg tests) | ~3 | ~250 | Med-high (spins `WorkflowEnvironment` to assert one arg) | Med | full pipeline already covered by `test_workflow_persists_on_high_score` |
| `products/batch_exports/.../test_metrics.py::test_sla_waiter` | Batch exports | FIX `asyncio.sleep(3)` (sla=1s) + DELETE histogram change-detector | 1 | ~60 | Med (real worker + PG + ClickHouse) | Med-high | |
| `products/tasks/.../process_task/tests/test_followup.py` | PostHog Code | FIX `asyncio.sleep(2)` → poll/time-skip (runs in CI, not Modal-gated) | 0 | — | Med-high | Med | |
| `products/surveys/.../test_survey.py:6316` | Surveys | FIX `time.sleep(1)` → `freeze_time` | 0 | — | Med | High | |

### Highest test/line-count opportunities (not yet shipped)

Big surface reductions; mostly cheap per-run, so they're a maintainability play more than a CI-time play. The two over-flagged areas are called out.

| Area / file(s) | Owner | Action | Tests | Lines | Conf | Notes |
|---|---|---|---|---|---|---|
| `products/warehouse_sources/**/test_*_source.py` | Warehouse sources | DELETE/MERGE templated change-detectors & constant-echoes | up to ~635 | ~5,500 | Mixed | **Do NOT blind-codemod.** Safe subset: `test_source_type` (~153, constant-echo) + `source_for_pipeline` plumbing change-detectors (preserve the incremental-drop branch). KEEP `test_single_secret_api_key_field` (security) and `test_get_source_config` (user-facing metadata). |
| `nodejs/src/ingestion/framework/docs/*.test.ts` | Ingestion | DELETE/MERGE redundant tutorial tests | up to ~65 | ~4,600 | Mixed | **Per-test triage required.** `docs/02` uniquely covers cardinality-mismatch-throws; move unique edges into the real unit test files before deleting. The pure mechanical dupes (e.g. `03`,`04`,`06`,`10`,`12`) are safer; `01`,`13` are the canonical onboarding spec (team call). |
| `frontend/.../mobile-replay/transform.test.ts` (+ `.snap` 9,001 lines) | Replay | FIX (selective) — keep schema-validated transforms, drop redundant wireframe→web blobs | — | large share of 9,001 | Med | needs per-case triage; mobile→web is genuinely snapshot-friendly |
| `frontend/.../marketing-analytics/.../utils.test.ts` (+ `.snap` 1,081) | Web/marketing analytics | FIX — assert key query fields instead of source×column snapshot matrix | 0 | ~1,081 | Med | otherwise exemplary file |
| `ee/hogai/**` (Max AI) various | Max AI | MERGE/parameterize + DELETE zero-assertion tests | ~110–120 | ~2,400 | Mixed | e.g. `parallel_task_execution/test_nodes.py` has tests with **no assertions** (DELETE, high conf); `upsert_dashboard/test_tool.py` reflow geometry (~9 → 1, heavy ORM); `read_data/test_tool.py` per-kind access pairs (MERGE); `context/test_context.py` mock-only tests (DELETE) |
| `ee/billing/test/test_salesforce_enrichment.py` | Billing | MERGE field-degradation clusters → 2 parameterized | ~20 | ~350 | High | also removes 39 banned doc-comments |
| `products/feature_flags/backend/test/test_flags_cache.py` | Feature flags | DELETE change-detectors (etag/dual-write/metric tombstone) | 5 | ~108 | Med | keep etag tests if a load-path test proves behavior |
| `products/logs/backend/test/test_logs_alerting_metrics.py` | Logs | DELETE mock-choreography (OTel meter mocked → nothing emitted) | ~16 | ~260 | High | keep checkpoint-lag clamp + ExecutionTimeRecorder status |
| `products/tasks/backend/tests/test_api.py` | PostHog Code | MERGE IDOR-404 family + command-proxy happy-paths; DELETE SQL-shape change-detectors | ~12 | ~217 | High | keep distinct elif arms (e.g. `create_run_for_other_user`) |
| `products/data_warehouse/backend/tests/api/*` | Data warehouse | MERGE row-filter rejection + host-change credential gating | ~11 | ~175 | Med-high | confirm validators are source-agnostic before merging |
| `products/conversations/backend/api/tests/*` | Conversations | MERGE attachment-skip + per-event token tests | ~7 | ~110 | High | keep positive paths + per-event property keys |
| `tools/**` (hogli/infra/pr-bots) | Dev tooling | DELETE tautological CLI tests + MERGE truth-table dupes | ~40 | ~430 | High | e.g. `test_cli.py` asserts `exit_code in (0,1,2)` (accepts everything); `test_devbox.py` pins exact internal call order |
| `rust/**` (kafka-deduplicator, cymbal, cohort-stream-processor) | Various | MERGE `#[rstest]` pairs + DELETE constructor/timing tests | ~10 | ~350 | Mixed | most `cohort-stream-processor/tests/*_consumer.rs` are `#[ignore]`d → ~0 CI cost (down-weighted) |
| `posthog/test/test_scopes.py`, `posthog/models/test/test_oauth.py`, `posthog/models/test/test_sharing_configuration_model.py` | Security / Product analytics | DELETE trivial/framework | 27 | 367 | High | **shipped** (PRs #66336, #66337) |
| `posthog/temporal/tests/common/*` | Platform | DELETE framework/redundant | 5 | 79 | High | **shipped** (PR #66339) |

### Validated `posthog/` worklist (folded in)

The provided worklist held 161 findings (83 DELETE, 27 MERGE, 51 FIX) across 59 files in the `posthog/` tree, each judged against the bar and adversarially re-checked. The highest-concentration clusters were re-validated here against current code; the act-ready, airtight ones were shipped (PRs #66336, #66337, #66339 above). The remainder are sound but lower-impact (near-zero CI cost, pure-unit / `SimpleTestCase`) and are good follow-ups for the owning teams:

- `posthog/models/scoping/test_root_mixin.py` — 3–4 DELETE + 1 FIX. **Excluded from shipping**: the redundancy claims are cross-file (`test_product_mixin.py`) and the file re-implements the production IDOR predicate; near-zero CI cost, security-adjacent → higher risk, negligible benefit. Recommend FIX (extract the predicate into importable production code and assert against it) over delete.
- `posthog/temporal/tests/session_replay/surfacing_scoring_sweep/{test_sql_alignment,test_scorer}.py` — ~7 DELETE (redundant parity/lazy-load checks). Valid but cheap pure-unit (synthetic booster + regex parse) → low CI impact. Team: Replay.
- `posthog/admin/test_data_deletion_request_admin.py` — 2 MERGE (short-circuit-identical paths) + 1 FIX (`test_preview_stats_rejects_non_clickhouse_team` passes even if the authz guard is removed — strengthen the assertion).
- `posthog/api/test/test_capture_internal.py`, `posthog/test/activity_logging/*`, `posthog/storage/test/*`, `posthog/clickhouse/client/test/test_limit.py`, `posthog/approvals/tests/*`, `posthog/management/commands/test/*` — assorted MERGE/FIX/DELETE, all low CI cost. See the worklist for per-test evidence.

The 51 worklist **FIX** items (flawed-but-valuable tests) are not surface reductions and weren't shipped; they're genuine quality improvements (e.g. assertions that pass even when the guard they claim to test is removed) worth picking up per file.

---

## Notable KEEPs (audited, deliberately not touched)

These look removable by heuristic but earn their place — listed so the same ground isn't re-flagged:

- **Error-classification tests** across `products/warehouse_sources` (`test_non_retryable_errors_*`, retryable/non-retryable parameterized) — the #1 real-bug cluster in `mistakes-we-make.md`. Assert real string→class mapping, both directions.
- **`validate_credentials` / secret-field tests** in warehouse sources — guard real branching and credential-exposure.
- **ClickHouse data-executing correctness tests** (`ee/clickhouse/.../test_cohort_query.py`, `test_property.py`, experiments `test_frequentist.py`/`test_bayesian.py`) — expensive but earned; they execute real SQL/stats.
- **IDOR / two-org scoping tests** — PostHog's standard tenant-isolation shape; keep.
- **`consumer-v1.test.ts`** (nodejs) — v1 is live behind the `CONSUMER_USE_V2` rollout flag; de-sleep, don't delete.
- **`test_handles_temporal_timeout`** (temporal combined-metrics) — catches the real 5s scrape-timeout regression; its cost would need an injectable timeout in production code to fix (out of scope for a test-only PR).
- **`*_consumer.rs` integration tests** (cohort-stream-processor) — `#[ignore]`d, so ~0 CI cost; valuable as manual broker tests.
- No genuine cross-language source-scraping was found in scope (the `read_text()`/`open()` calls read JSON/XML/SAML fixtures or AST-parse *same-language* source for a real CI optimization).

## Suggested sequencing for the rest

1. **Biggest CI wins first**, owned by their teams: the batch_exports Temporal matrix trims and the `ee/billing/test_quota_limiting.py` sleep-fix dwarf everything else in wall-time.
2. **Surgical warehouse_sources cleanup** (not a blind codemod): a single Data Warehouse PR removing `test_source_type` constant-echoes + `source_for_pipeline` plumbing change-detectors, preserving security/metadata/incremental-drop coverage.
3. **nodejs `framework/docs/` triage**: move unique edges (cardinality-mismatch, etc.) into the real unit files, then delete the pure dupes.
4. **Max AI (`ee/hogai`) and dev-tooling (`tools/`) merges**: large line/test reductions, low CI cost — good maintainability cleanup.
