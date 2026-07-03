import { EmailReputationService } from '../email-reputation.service'
import { EmailMetricsRow, EvaluationSummary, ReputationTransitionPayload } from '../types'

export interface EmailReputationActivities {
    fetchEmailMetrics: () => Promise<EmailMetricsRow[]>
    evaluateAndEnforce: (metrics: EmailMetricsRow[]) => Promise<EvaluationSummary>
    notifyTransitions: (transitions: ReputationTransitionPayload[]) => Promise<void>
}

/**
 * Activity payloads ride Temporal workflow history (~2 MiB cap per payload). The metric rows are
 * one small row per workflow that sent email in the window, so they stay far below the limit; if
 * per-recipient data is ever added, pass it by reference instead.
 */
export function createActivities(service: EmailReputationService): EmailReputationActivities {
    return {
        fetchEmailMetrics: () => service.fetchEmailMetrics(),
        evaluateAndEnforce: (metrics) => service.evaluateAndEnforce(metrics),
        notifyTransitions: (transitions) => service.notifyTransitions(transitions),
    }
}
