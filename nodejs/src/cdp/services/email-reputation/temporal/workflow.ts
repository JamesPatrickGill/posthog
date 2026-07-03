/**
 * Temporal workflow: one email-reputation evaluation pass. Started every
 * EMAIL_REPUTATION_EVALUATION_INTERVAL_MINUTES by a Temporal Schedule (overlap: SKIP).
 *
 * Runs in Temporal's deterministic workflow sandbox — keep this file free of runtime imports
 * other than @temporalio/workflow; all IO happens in the activities.
 */
import { proxyActivities } from '@temporalio/workflow'

import type { EmailReputationActivities } from './activities'

const { fetchEmailMetrics, evaluateAndEnforce, notifyTransitions } = proxyActivities<EmailReputationActivities>({
    startToCloseTimeout: '10 minutes',
    retry: {
        maximumAttempts: 3,
        initialInterval: '10s',
    },
})

export interface EmailReputationEvaluationResult {
    workflowsEvaluated: number
    teamsEvaluated: number
    transitions: number
}

export async function emailReputationEvaluation(): Promise<EmailReputationEvaluationResult> {
    const metrics = await fetchEmailMetrics()
    if (metrics.length === 0) {
        return { workflowsEvaluated: 0, teamsEvaluated: 0, transitions: 0 }
    }

    const summary = await evaluateAndEnforce(metrics)
    if (summary.transitions.length > 0) {
        await notifyTransitions(summary.transitions)
    }

    return {
        workflowsEvaluated: summary.workflowsEvaluated,
        teamsEvaluated: summary.teamsEvaluated,
        transitions: summary.transitions.length,
    }
}
