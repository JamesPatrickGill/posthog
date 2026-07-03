import { ClickHouseClient } from '@clickhouse/client'
import { randomUUID } from 'crypto'
import { Counter } from 'prom-client'

import { InternalFetchService } from '~/common/services/internal-fetch'
import { PostgresRouter, PostgresUse } from '~/common/utils/db/postgres'
import { logger } from '~/common/utils/logger'
import { PubSub } from '~/common/utils/pubsub'

import {
    DEFAULT_THRESHOLDS,
    ReputationDecision,
    ReputationThresholds,
    evaluateReputation,
    thresholdFor,
} from './state-machine'
import { EmailMetricsRow, EvaluationSummary, ReputationStateRow, ReputationTransitionPayload } from './types'

const reputationTransitionsCounter = new Counter({
    name: 'email_reputation_transitions_total',
    help: 'Email reputation state transitions applied by the evaluator',
    labelNames: ['scope', 'transition'],
})

interface HogFlowRow {
    id: string
    team_id: number
    name: string | null
    status: string
}

export interface EmailReputationServiceConfig {
    windowHours: number
    thresholds: ReputationThresholds
}

export const DEFAULT_EMAIL_REPUTATION_CONFIG: EmailReputationServiceConfig = {
    windowHours: 24,
    thresholds: DEFAULT_THRESHOLDS,
}

/**
 * Evaluates per-workflow and per-team email sender reputation from app_metrics2 and enforces it:
 * warned/paused states are persisted to posthog_emailreputationstate, breaching workflows are
 * flipped to status='paused' (with a reload-hog-flows publish so CDP workers drop them), and the
 * resulting transitions are reported to Django for in-app notifications.
 *
 * Designed to run as Temporal activities with a single evaluator instance (the Temporal schedule
 * uses overlap=SKIP), so read-modify-write on the reputation table needs no locking.
 */
export class EmailReputationService {
    constructor(
        private clickhouse: ClickHouseClient,
        private postgres: PostgresRouter,
        private pubSub: PubSub,
        private internalFetch: InternalFetchService,
        private config: EmailReputationServiceConfig = DEFAULT_EMAIL_REPUTATION_CONFIG
    ) {}

    public async fetchEmailMetrics(): Promise<EmailMetricsRow[]> {
        const result = await this.clickhouse.query({
            query: `
                SELECT
                    team_id,
                    app_source_id,
                    sumIf(count, metric_name = 'email_sent') AS sent,
                    sumIf(count, metric_name = 'email_bounced') AS bounced,
                    sumIf(count, metric_name = 'email_blocked') AS complained
                FROM app_metrics2
                WHERE app_source = 'hog_flow'
                    AND metric_kind = 'email'
                    AND metric_name IN ('email_sent', 'email_bounced', 'email_blocked')
                    AND timestamp >= now() - INTERVAL {windowHours:UInt32} HOUR
                GROUP BY team_id, app_source_id
                HAVING sent > 0
            `,
            query_params: { windowHours: this.config.windowHours },
            format: 'JSONEachRow',
        })
        const rows = await result.json<{
            team_id: number | string
            app_source_id: string
            sent: number | string
            bounced: number | string
            complained: number | string
        }>()

        return rows.map((row) => ({
            teamId: Number(row.team_id),
            appSourceId: row.app_source_id,
            sent: Number(row.sent),
            bounced: Number(row.bounced),
            complained: Number(row.complained),
        }))
    }

    public async evaluateAndEnforce(metrics: EmailMetricsRow[]): Promise<EvaluationSummary> {
        const now = new Date()
        const transitions: ReputationTransitionPayload[] = []
        const reloadFlowIdsByTeam = new Map<number, string[]>()

        const teamIds = [...new Set(metrics.map((m) => m.teamId))]
        const { flows, metricsByFlowId } = await this.attributeMetricsToFlows(metrics)
        const existing = await this.fetchReputationRows(teamIds)
        const workflowRowByFlowId = new Map(
            existing.filter((r) => r.hog_flow_id !== null).map((r) => [r.hog_flow_id as string, r])
        )
        const teamRowByTeamId = new Map(existing.filter((r) => r.hog_flow_id === null).map((r) => [r.team_id, r]))

        let workflowsEvaluated = 0
        for (const [flowId, flowMetrics] of metricsByFlowId) {
            const flow = flows.get(flowId)
            if (!flow) {
                continue
            }
            workflowsEvaluated++

            const current = workflowRowByFlowId.get(flow.id)
            const decision = evaluateReputation(
                {
                    currentState: current?.state ?? 'healthy',
                    warnedAt: current?.warned_at ? new Date(current.warned_at) : null,
                    metrics: flowMetrics,
                    now,
                },
                this.config.thresholds
            )

            let applied = decision
            let previousFlowStatus: string | null = null
            if (decision.transition === 'pause') {
                const flipped = await this.pauseFlows([flow.id])
                if (flipped.length > 0) {
                    previousFlowStatus = 'active'
                    this.appendReload(reloadFlowIdsByTeam, flow.team_id, flipped)
                } else {
                    // The flow isn't active (nothing to stop), so a pause would only create a state the
                    // user can't re-enable. Hold the previous state; if it re-activates while rates are
                    // still bad, the next evaluation pauses it for real.
                    applied = { ...decision, nextState: current?.state ?? 'healthy', transition: 'none', reason: null }
                }
            }

            await this.persistRow({
                existing: current ?? null,
                teamId: flow.team_id,
                hogFlowId: flow.id,
                scope: 'workflow',
                decision: applied,
                emailsSent: flowMetrics.sent,
                now,
                previousFlowStatus,
            })

            if (applied.transition === 'warn' || applied.transition === 'pause') {
                reputationTransitionsCounter.labels('workflow', applied.transition).inc()
                transitions.push({
                    team_id: flow.team_id,
                    scope: 'workflow',
                    new_state: applied.nextState as 'warned' | 'paused',
                    reason: applied.reason!,
                    rate: applied.reason === 'complaint' ? applied.complaintRate : applied.bounceRate,
                    threshold: thresholdFor(applied.transition, applied.reason, this.config.thresholds),
                    hog_flow_id: flow.id,
                    hog_flow_name: flow.name ?? undefined,
                })
            }
        }

        for (const teamId of teamIds) {
            const teamMetrics = metrics.filter((m) => m.teamId === teamId)
            const totals = {
                sent: teamMetrics.reduce((acc, m) => acc + m.sent, 0),
                bounced: teamMetrics.reduce((acc, m) => acc + m.bounced, 0),
                complained: teamMetrics.reduce((acc, m) => acc + m.complained, 0),
            }
            const current = teamRowByTeamId.get(teamId)
            const decision = evaluateReputation(
                {
                    currentState: current?.state ?? 'healthy',
                    warnedAt: current?.warned_at ? new Date(current.warned_at) : null,
                    metrics: totals,
                    now,
                },
                this.config.thresholds
            )

            if (decision.transition === 'pause') {
                const flipped = await this.pauseAllEmailFlowsForTeam(teamId)
                if (flipped.length > 0) {
                    this.appendReload(reloadFlowIdsByTeam, teamId, flipped)
                }
                // Materialize a paused row per flipped flow so each one carries previous_flow_status
                // and can be individually re-enabled; the org-level notification covers them all.
                for (const flowId of flipped) {
                    await this.persistRow({
                        existing: workflowRowByFlowId.get(flowId) ?? null,
                        teamId,
                        hogFlowId: flowId,
                        scope: 'workflow',
                        decision,
                        emailsSent: metricsByFlowId.get(flowId)?.sent ?? 0,
                        now,
                        previousFlowStatus: 'active',
                    })
                }
            }

            await this.persistRow({
                existing: current ?? null,
                teamId,
                hogFlowId: null,
                scope: 'team',
                decision,
                emailsSent: totals.sent,
                now,
                previousFlowStatus: null,
            })

            if (decision.transition === 'warn' || decision.transition === 'pause') {
                reputationTransitionsCounter.labels('team', decision.transition).inc()
                transitions.push({
                    team_id: teamId,
                    scope: 'team',
                    new_state: decision.nextState as 'warned' | 'paused',
                    reason: decision.reason!,
                    rate: decision.reason === 'complaint' ? decision.complaintRate : decision.bounceRate,
                    threshold: thresholdFor(decision.transition, decision.reason, this.config.thresholds),
                })
            }
        }

        for (const [teamId, hogFlowIds] of reloadFlowIdsByTeam) {
            await this.pubSub.publish('reload-hog-flows', JSON.stringify({ teamId, hogFlowIds }))
        }

        if (transitions.length > 0) {
            logger.info('[EmailReputation] applied transitions', { transitions })
        }

        return { workflowsEvaluated, teamsEvaluated: teamIds.length, transitions }
    }

    public async notifyTransitions(transitions: ReputationTransitionPayload[]): Promise<void> {
        const { fetchError, fetchResponse } = await this.internalFetch.fetch({
            urlPath: '/api/internal/hog_flows/email_reputation_notify',
            fetchParams: {
                method: 'POST',
                body: JSON.stringify({ transitions }),
            },
        })
        if (fetchError || !fetchResponse || fetchResponse.status >= 400) {
            throw new Error(
                `Failed to deliver reputation notifications: ${fetchError?.message ?? `status ${fetchResponse?.status}`}`
            )
        }
    }

    /**
     * Resolve metric rows to workflows and aggregate per workflow. Batch-triggered runs record
     * metrics under the batch-job id (`parentRunId`), not the workflow id — and batch broadcasts are
     * the highest-risk email blasts — so unmatched app_source_ids are resolved through
     * posthog_hogflowbatchjob and folded into the parent workflow's numbers. Ids matching neither
     * (deleted flows, plain hog functions) still count toward the team aggregate.
     */
    private async attributeMetricsToFlows(metrics: EmailMetricsRow[]): Promise<{
        flows: Map<string, HogFlowRow>
        metricsByFlowId: Map<string, { sent: number; bounced: number; complained: number }>
    }> {
        const sourceIds = [...new Set(metrics.map((m) => m.appSourceId))]
        const flows = await this.fetchHogFlows(sourceIds)

        const unmatched = sourceIds.filter((id) => !flows.has(id))
        const batchJobToFlow = await this.fetchBatchJobFlowIds(unmatched)
        const extraFlowIds = [...new Set([...batchJobToFlow.values()])].filter((id) => !flows.has(id))
        for (const [id, flow] of await this.fetchHogFlows(extraFlowIds)) {
            flows.set(id, flow)
        }

        const metricsByFlowId = new Map<string, { sent: number; bounced: number; complained: number }>()
        for (const metric of metrics) {
            const flowId = flows.has(metric.appSourceId) ? metric.appSourceId : batchJobToFlow.get(metric.appSourceId)
            if (!flowId || !flows.has(flowId)) {
                continue
            }
            const acc = metricsByFlowId.get(flowId) ?? { sent: 0, bounced: 0, complained: 0 }
            acc.sent += metric.sent
            acc.bounced += metric.bounced
            acc.complained += metric.complained
            metricsByFlowId.set(flowId, acc)
        }
        return { flows, metricsByFlowId }
    }

    private async fetchBatchJobFlowIds(ids: string[]): Promise<Map<string, string>> {
        if (ids.length === 0) {
            return new Map()
        }
        const result = await this.postgres.query<{ id: string; hog_flow_id: string }>(
            PostgresUse.COMMON_READ,
            `SELECT id, hog_flow_id FROM posthog_hogflowbatchjob WHERE id = ANY($1)`,
            [ids],
            'emailReputationFetchBatchJobs'
        )
        return new Map(result.rows.map((row) => [row.id, row.hog_flow_id]))
    }

    private async fetchHogFlows(ids: string[]): Promise<Map<string, HogFlowRow>> {
        if (ids.length === 0) {
            return new Map()
        }
        const result = await this.postgres.query<HogFlowRow>(
            PostgresUse.COMMON_READ,
            `SELECT id, team_id, name, status FROM posthog_hogflow WHERE id = ANY($1)`,
            [ids],
            'emailReputationFetchHogFlows'
        )
        return new Map(result.rows.map((row) => [row.id, row]))
    }

    private async fetchReputationRows(teamIds: number[]): Promise<ReputationStateRow[]> {
        if (teamIds.length === 0) {
            return []
        }
        const result = await this.postgres.query<ReputationStateRow>(
            PostgresUse.COMMON_READ,
            `SELECT id, team_id, hog_flow_id, scope, state, warned_at
             FROM posthog_emailreputationstate WHERE team_id = ANY($1)`,
            [teamIds],
            'emailReputationFetchState'
        )
        return result.rows
    }

    /** Flip the given flows to paused, but only those currently active. Returns the ids actually flipped. */
    private async pauseFlows(flowIds: string[]): Promise<string[]> {
        const result = await this.postgres.query<{ id: string }>(
            PostgresUse.COMMON_WRITE,
            `UPDATE posthog_hogflow SET status = 'paused', updated_at = now()
             WHERE id = ANY($1) AND status = 'active' RETURNING id`,
            [flowIds],
            'emailReputationPauseFlows'
        )
        return result.rows.map((row) => row.id)
    }

    private async pauseAllEmailFlowsForTeam(teamId: number): Promise<string[]> {
        const result = await this.postgres.query<{ id: string }>(
            PostgresUse.COMMON_WRITE,
            `UPDATE posthog_hogflow SET status = 'paused', updated_at = now()
             WHERE team_id = $1 AND status = 'active' AND billable_action_types @> '["function_email"]'::jsonb
             RETURNING id`,
            [teamId],
            'emailReputationPauseTeamFlows'
        )
        return result.rows.map((row) => row.id)
    }

    private appendReload(map: Map<number, string[]>, teamId: number, flowIds: string[]): void {
        map.set(teamId, [...(map.get(teamId) ?? []), ...flowIds])
    }

    private async persistRow(params: {
        existing: ReputationStateRow | null
        teamId: number
        hogFlowId: string | null
        scope: 'workflow' | 'team'
        decision: ReputationDecision
        emailsSent: number
        now: Date
        previousFlowStatus: string | null
    }): Promise<void> {
        const { existing, teamId, hogFlowId, scope, decision, emailsSent, now, previousFlowStatus } = params
        const { nextState, transition, reason, bounceRate, complaintRate } = decision

        const changed = transition !== 'none'
        const warnedAt = transition === 'warn' ? now : transition === 'clear_warning' ? null : undefined
        const pausedAt = transition === 'pause' ? now : undefined

        if (existing) {
            const setFragments = [
                'state = $2',
                'bounce_rate = $3',
                'complaint_rate = $4',
                'emails_sent = $5',
                'window_end = $6',
                'evaluated_at = $6',
                'updated_at = $6',
            ]
            const values: any[] = [existing.id, nextState, bounceRate, complaintRate, emailsSent, now]
            if (changed) {
                setFragments.push('state_changed_at = $6')
            }
            if (warnedAt !== undefined) {
                values.push(warnedAt)
                setFragments.push(`warned_at = $${values.length}`)
            }
            if (pausedAt !== undefined) {
                values.push(pausedAt)
                setFragments.push(`paused_at = $${values.length}`)
                values.push(reason)
                setFragments.push(`pause_reason = $${values.length}`)
                values.push(previousFlowStatus)
                setFragments.push(`previous_flow_status = $${values.length}`)
            }
            await this.postgres.query(
                PostgresUse.COMMON_WRITE,
                `UPDATE posthog_emailreputationstate SET ${setFragments.join(', ')} WHERE id = $1`,
                values,
                'emailReputationUpdateState'
            )
        } else {
            // Upsert against the partial unique indexes: a row for the same target can be created twice
            // within one run (per-workflow evaluation, then a team-level pause of the same flow).
            const conflictTarget =
                hogFlowId !== null
                    ? '(team_id, hog_flow_id) WHERE hog_flow_id IS NOT NULL'
                    : '(team_id) WHERE hog_flow_id IS NULL'
            await this.postgres.query(
                PostgresUse.COMMON_WRITE,
                `INSERT INTO posthog_emailreputationstate
                    (id, team_id, hog_flow_id, scope, state, bounce_rate, complaint_rate, emails_sent,
                     window_end, evaluated_at, state_changed_at, warned_at, paused_at, pause_reason,
                     previous_flow_status, created_at, updated_at)
                 VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $9, $10, $11, $12, $13, $14, $9, $9)
                 ON CONFLICT ${conflictTarget} DO UPDATE SET
                    state = EXCLUDED.state,
                    bounce_rate = EXCLUDED.bounce_rate,
                    complaint_rate = EXCLUDED.complaint_rate,
                    emails_sent = EXCLUDED.emails_sent,
                    window_end = EXCLUDED.window_end,
                    evaluated_at = EXCLUDED.evaluated_at,
                    state_changed_at = COALESCE(EXCLUDED.state_changed_at, posthog_emailreputationstate.state_changed_at),
                    warned_at = EXCLUDED.warned_at,
                    paused_at = COALESCE(EXCLUDED.paused_at, posthog_emailreputationstate.paused_at),
                    pause_reason = COALESCE(EXCLUDED.pause_reason, posthog_emailreputationstate.pause_reason),
                    previous_flow_status = COALESCE(EXCLUDED.previous_flow_status, posthog_emailreputationstate.previous_flow_status),
                    updated_at = EXCLUDED.updated_at`,
                [
                    randomUUID(),
                    teamId,
                    hogFlowId,
                    scope,
                    nextState,
                    bounceRate,
                    complaintRate,
                    emailsSent,
                    now,
                    changed ? now : null,
                    warnedAt ?? null,
                    pausedAt ?? null,
                    pausedAt !== undefined ? reason : null,
                    previousFlowStatus,
                ],
                'emailReputationUpsertState'
            )
        }
    }
}
