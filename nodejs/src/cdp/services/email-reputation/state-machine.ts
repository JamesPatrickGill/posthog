/**
 * Pure state machine for email sender reputation, evaluated per workflow and per team.
 *
 * States mirror `posthog_emailreputationstate.state`:
 * - healthy → warned: a soft threshold was crossed
 * - warned → healthy: rates dropped back below all soft thresholds (warnings auto-clear)
 * - warned → paused: still above a soft threshold after the grace period, or a hard threshold crossed
 * - healthy → paused: a hard threshold crossed outright
 * - paused: terminal — only the manual re-enable endpoint (Django) leaves it
 */

export type ReputationState = 'healthy' | 'warned' | 'paused'
export type ReputationReason = 'bounce' | 'complaint'
export type ReputationTransition = 'none' | 'warn' | 'pause' | 'clear_warning'

export interface ReputationThresholds {
    /** Below this many sends in the window, rates are too noisy to act on. */
    minSends: number
    bounceWarn: number
    bouncePause: number
    complaintWarn: number
    complaintPause: number
    /** Minutes a warning must persist before it escalates to a pause. With a rolling
     * 24h window the rate can't recover within one evaluation cycle, so escalating on the
     * literal next evaluation would give senders no time to react to the warning. */
    warnGraceMinutes: number
}

// Warn well before AWS's account-review lines (5% bounce / 0.1% complaint at ~0.5% escalation).
export const DEFAULT_THRESHOLDS: ReputationThresholds = {
    minSends: 100,
    bounceWarn: 0.02,
    bouncePause: 0.05,
    complaintWarn: 0.001,
    complaintPause: 0.005,
    warnGraceMinutes: 120,
}

export interface ReputationMetrics {
    sent: number
    bounced: number
    complained: number
}

export interface ReputationEvaluationInput {
    currentState: ReputationState
    /** When the current warning started; null unless currentState is 'warned'. */
    warnedAt: Date | null
    metrics: ReputationMetrics
    now: Date
}

export interface ReputationDecision {
    nextState: ReputationState
    transition: ReputationTransition
    /** Which signal drove a warn/pause; null for none/clear_warning. */
    reason: ReputationReason | null
    bounceRate: number
    complaintRate: number
}

function decide(
    input: ReputationEvaluationInput,
    bounceRate: number,
    complaintRate: number,
    thresholds: ReputationThresholds
): Pick<ReputationDecision, 'nextState' | 'transition' | 'reason'> {
    const { currentState, warnedAt, metrics, now } = input

    // Paused is terminal for the evaluator; only manual re-enable exits it.
    if (currentState === 'paused') {
        return { nextState: 'paused', transition: 'none', reason: null }
    }

    // Too little volume to judge: hold the current state rather than flapping.
    if (metrics.sent < thresholds.minSends) {
        return { nextState: currentState, transition: 'none', reason: null }
    }

    // Complaints are the more dangerous SES signal, so they win when both breach.
    const hardReason: ReputationReason | null =
        complaintRate >= thresholds.complaintPause
            ? 'complaint'
            : bounceRate >= thresholds.bouncePause
              ? 'bounce'
              : null
    const softReason: ReputationReason | null =
        complaintRate >= thresholds.complaintWarn ? 'complaint' : bounceRate >= thresholds.bounceWarn ? 'bounce' : null

    if (hardReason) {
        return { nextState: 'paused', transition: 'pause', reason: hardReason }
    }

    if (currentState === 'healthy') {
        return softReason
            ? { nextState: 'warned', transition: 'warn', reason: softReason }
            : { nextState: 'healthy', transition: 'none', reason: null }
    }

    // currentState === 'warned'
    if (!softReason) {
        return { nextState: 'healthy', transition: 'clear_warning', reason: null }
    }
    const graceMs = thresholds.warnGraceMinutes * 60 * 1000
    const graceElapsed = warnedAt !== null && now.getTime() - warnedAt.getTime() >= graceMs
    return graceElapsed
        ? { nextState: 'paused', transition: 'pause', reason: softReason }
        : { nextState: 'warned', transition: 'none', reason: null }
}

export function evaluateReputation(
    input: ReputationEvaluationInput,
    thresholds: ReputationThresholds = DEFAULT_THRESHOLDS
): ReputationDecision {
    const { metrics } = input
    const bounceRate = metrics.sent > 0 ? metrics.bounced / metrics.sent : 0
    const complaintRate = metrics.sent > 0 ? metrics.complained / metrics.sent : 0

    return {
        ...decide(input, bounceRate, complaintRate, thresholds),
        bounceRate,
        complaintRate,
    }
}

/** The threshold the decision was judged against, for notification copy. */
export function thresholdFor(
    transition: ReputationTransition,
    reason: ReputationReason | null,
    thresholds: ReputationThresholds = DEFAULT_THRESHOLDS
): number {
    if (reason === 'complaint') {
        return transition === 'pause' ? thresholds.complaintPause : thresholds.complaintWarn
    }
    return transition === 'pause' ? thresholds.bouncePause : thresholds.bounceWarn
}
