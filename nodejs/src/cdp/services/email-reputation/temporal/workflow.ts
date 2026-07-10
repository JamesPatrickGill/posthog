/**
 * Temporal workflow: one daily email-reputation snapshot run, started by a Temporal Schedule
 * (overlap: SKIP). Teams are evaluated in paced batches with durable sleeps in between, so the
 * sweep doesn't hit ClickHouse/Postgres in one burst and resumes mid-run after a worker restart.
 *
 * Runs in Temporal's deterministic workflow sandbox — keep this file free of runtime imports
 * other than @temporalio/workflow; all IO happens in the activities.
 */
import { proxyActivities, sleep } from '@temporalio/workflow'

import type { EmailReputationActivities } from './activities'

const { fetchTeamsToEvaluate, evaluateTeamBatch } = proxyActivities<EmailReputationActivities>({
    startToCloseTimeout: '10 minutes',
    retry: {
        maximumAttempts: 3,
        initialInterval: '10s',
    },
})

export interface EmailReputationEvaluationResult {
    evaluatedAt: string
    teamsEvaluated: number
    workflowsEvaluated: number
    snapshotsWritten: number
}

export async function emailReputationEvaluation(): Promise<EmailReputationEvaluationResult> {
    // Captured once before any awaits: every batch (and any retry) shares one run timestamp, which
    // both anchors the metrics window and dedupes re-inserted snapshot rows. The sandbox makes
    // `new Date()` deterministic on replay.
    const evaluatedAt = new Date().toISOString()

    const plan = await fetchTeamsToEvaluate(evaluatedAt)
    const result: EmailReputationEvaluationResult = {
        evaluatedAt,
        teamsEvaluated: 0,
        workflowsEvaluated: 0,
        snapshotsWritten: 0,
    }

    const batchSize = Math.max(1, plan.batchSize)
    for (let offset = 0; offset < plan.teamIds.length; offset += batchSize) {
        if (offset > 0 && plan.batchDelayMs > 0) {
            await sleep(plan.batchDelayMs)
        }
        const summary = await evaluateTeamBatch(plan.teamIds.slice(offset, offset + batchSize), evaluatedAt)
        result.teamsEvaluated += summary.teamsEvaluated
        result.workflowsEvaluated += summary.workflowsEvaluated
        result.snapshotsWritten += summary.snapshotsWritten
    }

    return result
}
