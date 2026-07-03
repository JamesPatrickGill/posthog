import { ReputationReason, ReputationState } from './state-machine'

/** Per-workflow email metrics aggregated from app_metrics2 over the evaluation window.
 * `appSourceId` is usually a HogFlow id but can be a batch-job id; rows that don't match a
 * HogFlow still count toward the team aggregate. */
export interface EmailMetricsRow {
    teamId: number
    appSourceId: string
    sent: number
    bounced: number
    complained: number
}

/** A state change the evaluator applied, shaped for the Django internal notify endpoint. */
export interface ReputationTransitionPayload {
    team_id: number
    scope: 'workflow' | 'team'
    new_state: 'warned' | 'paused'
    reason: ReputationReason
    rate: number
    threshold: number
    hog_flow_id?: string
    hog_flow_name?: string
}

export interface EvaluationSummary {
    workflowsEvaluated: number
    teamsEvaluated: number
    transitions: ReputationTransitionPayload[]
}

/** Row shape of posthog_emailreputationstate as read via pg (timestamps are ISO strings). */
export interface ReputationStateRow {
    id: string
    team_id: number
    hog_flow_id: string | null
    scope: 'workflow' | 'team'
    state: ReputationState
    warned_at: string | null
}
